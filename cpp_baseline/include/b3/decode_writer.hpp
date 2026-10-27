// decode_writer.hpp: the full decode writer, a walker sink writing JSON Lines.
//
// What it does:
//   One JSON line per message, in the shape of the generator's intent.jsonl:
//   channel, sequenceVersion, sequenceNumber, index, sendingTime, templateId,
//   blockLength, version, fields, null, and absent when not empty. Values are
//   written the way intent.jsonl writes them: integers as numbers (prices as
//   raw mantissas, enums and sets as their wire value), char and char[N] as
//   strings ("" for NUL), var data as strings. The last part of a News also
//   carries "news": {"headline", "text"} with the parts' text joined, as
//   intent.jsonl does. Unknown templates get fields {"body": "<hex>"}.
//   Groups and multi-member composites, which intent.jsonl never holds, use
//   the format of docs/DESIGN.md 3.6.2. A body fault (DESIGN.md 3.6.2) adds
//   "body_fault" and "undecoded" to the row and is counted by reason.
//   No instrument name: the C++ does not read the generator's config.
//
// What it must not do:
//   Run in timed code. It allocates and prints, so it is kept out of the
//   hot-path headers: packet.hpp, walker.hpp and decode.hpp never include it.

#pragma once

#include <array>
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

#include "b3/decode.hpp"
#include "b3/fields.hpp"
#include "b3/walker.hpp"

namespace b3::decode_writer {

// ---- JSON text helpers (src/decode_writer.cpp) ----
void append_string(std::string& out, std::string_view s);   // quoted, escaped
void append_hex_string(std::string& out, const std::uint8_t* p, std::size_t n);
void append_uint(std::string& out, std::uint64_t v);
void append_int(std::string& out, std::int64_t v);
void append_name_list(std::string& out, const std::vector<std::string_view>& names);

template <class T>
void append_number(std::string& out, T v) {
    if constexpr (std::is_signed_v<T>) {
        append_int(out, static_cast<std::int64_t>(v));
    } else {
        append_uint(out, static_cast<std::uint64_t>(v));
    }
}

// One JSON object of fields being written, with its null and absent lists.
struct FieldsJson {
    std::string& out;
    std::vector<std::string_view> null{};
    std::vector<std::string_view> absent{};
    bool first = true;

    void key(std::string_view name) {
        if (!first) out += ',';
        first = false;
        append_string(out, name);
        out += ':';
    }
};

// Member M of a composite: its wire value. Returns whether it is null.
template <class M>
bool write_member(std::string& out, bool& first, const decode::Block& inner) {
    if (!first) out += ',';
    first = false;
    append_string(out, M::name);
    out += ':';
    const auto v = decode::get<M>(inner);
    append_number(out, v.value);
    return v.null();
}

// Writes field F of block b (an absent field only goes to j.absent).
template <class F>
void write_field(FieldsJson& j, const decode::Block& b) {
    using decode::Kind;
    if constexpr (F::kind == Kind::composite) {
        if (!decode::exists<F>(b)) {
            j.absent.push_back(F::name);
            return;
        }
        // {member: wire value}; null when optional and every member is null.
        j.key(F::name);
        j.out += '{';
        bool first = true;
        const auto inner = decode::composite_block<F>(b);
        bool all_null = true;
        [&]<class... Ms>(decode::List<Ms...>) {
            // Comma fold: members are written left to right.
            ((all_null = write_member<Ms>(j.out, first, inner) && all_null), ...);
        }(typename F::members{});
        j.out += '}';
        if (F::optional && all_null) j.null.push_back(F::name);
    } else {
        const auto v = decode::get<F>(b);
        if (v.absent()) {
            j.absent.push_back(F::name);
            return;
        }
        j.key(F::name);
        if constexpr (F::kind == Kind::char_array) {
            append_string(j.out, v.value);
        } else if constexpr (F::kind == Kind::character) {
            append_string(j.out, v.value == 0 ? std::string_view() : std::string_view(&v.value, 1));
        } else {
            append_number(j.out, v.value);
        }
        if (v.null()) j.null.push_back(F::name);
    }
}

template <class... Fs>
void write_fields(FieldsJson& j, const decode::Block& b, decode::List<Fs...>) {
    (write_field<Fs>(j, b), ...);
}

// Names of the groups, then var data, of a template, in wire order.
template <class... Gs, class... Ds>
std::vector<std::string_view> body_names(decode::List<Gs...>, decode::List<Ds...>) {
    return {Gs::name..., Ds::name...};
}

// Collects a message's groups and var data into its fields object.
struct BodyJson {
    FieldsJson& j;
    std::string_view headline{};    // News only: for the reassembled "news"
    std::string_view text{};
    bool faulted = false;
    decode::BodyFault fault{};
    std::string_view fault_at{};
    bool in_group = false;
    bool first_entry = true;

    template <class G>
    void on_group(std::size_t, std::size_t) {
        close_group();
        j.key(G::name);
        j.out += '[';
        in_group = true;
        first_entry = true;
    }
    // A group entry: {"fields": {...}, "null": [...], "absent": [...]}.
    template <class G>
    void on_entry(std::size_t, const decode::Block& entry) {
        j.out += first_entry ? "{\"fields\":{" : ",{\"fields\":{";
        first_entry = false;
        FieldsJson e{j.out};
        write_fields(e, entry, typename G::fields{});
        j.out += "},\"null\":";
        append_name_list(j.out, e.null);
        j.out += ",\"absent\":";
        append_name_list(j.out, e.absent);
        j.out += '}';
    }
    template <class D>
    void on_var_data(std::string_view bytes) {
        close_group();
        j.key(D::name);
        append_string(j.out, bytes);
        if (D::name == "headline") headline = bytes;
        if (D::name == "text") text = bytes;
    }
    template <class Where>
    void on_body_fault(decode::BodyFault reason) {
        faulted = true;
        fault = reason;
        fault_at = Where::name;
    }
    void close_group() {
        if (in_group) j.out += ']';
        in_group = false;
    }
};

class DecodeWriter {
public:
    explicit DecodeWriter(std::FILE* out) : out_(out) {}

    template <std::uint16_t TemplateId>
    void on(const walker::MessageView& m) {
        using T = fields::Template<TemplateId>;
        begin_row(m);
        FieldsJson j{line_};
        write_fields(j, decode::root(m), typename T::fields{});
        BodyJson body{j};
        decode::walk_body<T>(m, body);
        body.close_group();
        line_ += "},\"null\":";
        append_name_list(line_, j.null);
        if (!j.absent.empty()) {
            line_ += ",\"absent\":";
            append_name_list(line_, j.absent);
        }
        if constexpr (TemplateId == layout::News_5::template_id) {
            if (body.faulted) {
                news_by_channel_.erase(m.packet.channel);   // its text is incomplete: no "news"
            } else {
                news(m, body);
            }
        }
        if (body.faulted) {
            ++body_faults_[static_cast<std::size_t>(body.fault)];
            // The faulting group or var data, and everything after it.
            std::vector<std::string_view> undecoded;
            bool reached = false;
            for (const auto name : body_names(typename T::groups{}, typename T::var_data{})) {
                reached = reached || name == body.fault_at;
                if (reached) undecoded.push_back(name);
            }
            line_ += ",\"body_fault\":{\"reason\":";
            append_string(line_, decode::kBodyFaultNames[static_cast<std::size_t>(body.fault)]);
            line_ += ",\"at\":";
            append_string(line_, body.fault_at);
            line_ += "},\"undecoded\":";
            append_name_list(line_, undecoded);
        }
        end_row();
    }

    void on_unknown(const walker::MessageView& m);

    const std::array<std::uint64_t, decode::kBodyFaults>& body_faults() const { return body_faults_; }
    std::uint64_t rows() const { return rows_; }

private:
    void begin_row(const walker::MessageView& m);   // up to and including "fields":{
    void end_row();                                 // closes the row and writes it
    void news(const walker::MessageView& m, const BodyJson& body);

    // News parts on one channel (LAYOUTS.md 12.10): part 1 opens, the text of
    // parts 1..partCount in order is joined, the last part closes.
    struct NewsState {
        std::uint16_t next_part = 1;
        std::string headline;
        std::string text;
    };

    std::FILE* out_;
    std::string line_;
    std::map<std::uint8_t, NewsState> news_by_channel_;
    std::array<std::uint64_t, decode::kBodyFaults> body_faults_{};
    std::uint64_t rows_ = 0;
};

}  // namespace b3::decode_writer
