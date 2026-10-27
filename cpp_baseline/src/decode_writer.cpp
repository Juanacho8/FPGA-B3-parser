// decode_writer.cpp: the non-template parts of the full decode writer.
//
// What it does:
//   JSON text helpers (string escaping, hex, numbers), the row header and
//   end, unknown templates (fields {"body": hex}) and News reassembly
//   (see include/b3/decode_writer.hpp).
//
// What it must not do:
//   Be linked into timed code paths; it allocates and writes to a FILE.

#include "b3/decode_writer.hpp"

#include <cinttypes>

namespace b3::decode_writer {

void append_string(std::string& out, std::string_view s) {
    static constexpr char kHex[] = "0123456789abcdef";
    out += '"';
    for (const char c : s) {
        const auto u = static_cast<unsigned char>(c);
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (u < 0x20) {
                    out += "\\u00";
                    out += kHex[u >> 4];
                    out += kHex[u & 0xF];
                } else {
                    out += c;   // ASCII, or UTF-8 bytes passed through
                }
        }
    }
    out += '"';
}

void append_hex_string(std::string& out, const std::uint8_t* p, std::size_t n) {
    static constexpr char kHex[] = "0123456789abcdef";
    out += '"';
    for (std::size_t i = 0; i < n; ++i) {
        out += kHex[p[i] >> 4];
        out += kHex[p[i] & 0xF];
    }
    out += '"';
}

void append_uint(std::string& out, std::uint64_t v) {
    char buf[24];
    const int n = std::snprintf(buf, sizeof buf, "%" PRIu64, v);
    out.append(buf, static_cast<std::size_t>(n));
}

void append_int(std::string& out, std::int64_t v) {
    char buf[24];
    const int n = std::snprintf(buf, sizeof buf, "%" PRId64, v);
    out.append(buf, static_cast<std::size_t>(n));
}

void append_name_list(std::string& out, const std::vector<std::string_view>& names) {
    out += '[';
    for (std::size_t i = 0; i < names.size(); ++i) {
        if (i) out += ',';
        append_string(out, names[i]);
    }
    out += ']';
}

void DecodeWriter::begin_row(const walker::MessageView& m) {
    line_.clear();
    line_ += "{\"channel\":";
    append_uint(line_, m.packet.channel);
    line_ += ",\"sequenceVersion\":";
    append_uint(line_, m.packet.seq_version);
    line_ += ",\"sequenceNumber\":";
    append_uint(line_, m.packet.seq_num);
    line_ += ",\"index\":";
    append_uint(line_, m.msg_index);
    line_ += ",\"sendingTime\":";
    append_uint(line_, m.packet.sending_time);
    line_ += ",\"templateId\":";
    append_uint(line_, m.template_id);
    line_ += ",\"blockLength\":";
    append_uint(line_, m.block_length);
    line_ += ",\"version\":";
    append_uint(line_, m.version);
    line_ += ",\"fields\":{";
}

void DecodeWriter::end_row() {
    line_ += "}\n";
    std::fwrite(line_.data(), 1, line_.size(), out_);
    ++rows_;
}

void DecodeWriter::on_unknown(const walker::MessageView& m) {
    begin_row(m);
    line_ += "\"body\":";
    append_hex_string(line_, m.body, m.message_length - walker::mh::size);
    line_ += "},\"null\":[]";
    end_row();
}

void DecodeWriter::news(const walker::MessageView& m, const BodyJson& body) {
    namespace N = fields::News_5;
    const auto part_count = decode::get<N::partCount>(m);
    const auto part_number = decode::get<N::partNumber>(m);
    if (!part_count.present() || !part_number.present()) return;
    NewsState& st = news_by_channel_[m.packet.channel];
    if (part_number.value == 1) {
        st = NewsState{2, std::string(body.headline), std::string(body.text)};
    } else if (part_number.value == st.next_part) {
        st.text.append(body.text);
        ++st.next_part;
    } else {
        st = NewsState{};   // out of order or a part missing: no reassembled text
        return;
    }
    if (part_number.value == part_count.value) {
        line_ += ",\"news\":{\"headline\":";
        append_string(line_, st.headline);
        line_ += ",\"text\":";
        append_string(line_, st.text);
        line_ += '}';
        st = NewsState{};
    }
}

}  // namespace b3::decode_writer
