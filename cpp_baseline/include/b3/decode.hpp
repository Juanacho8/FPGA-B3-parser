// decode.hpp: field access for one message, with B3's presence rules.
//
// What it does:
//   get<F>(view) is the accessor for any field F described in fields.hpp (for
//   example b3::fields::Order_MBO_50::mDEntryPx). It reads the field at its
//   layouts.hpp offset with a wire.hpp load and reports one of three states
//   (docs/DESIGN.md 3.6):
//     absent   offset + size > blockLength, or sinceVersion > the header's version
//     null     present, and the wire value equals the schema's null value
//     present  anything else
//   Extra root-block bytes beyond the known fields are never looked at.
//   walk_body<T>(view, visitor) walks a template's groups and var data:
//   groups start at the header's blockLength, entries use the blockLength the
//   wire gives, var data follows. Every read is checked against
//   messageLength; the first one that does not fit is a body fault
//   (DESIGN.md 3.6.2), reported to the visitor, and the walk stops.
//
// What it must not do:
//   Allocate, make virtual calls, or read past messageLength. Constant fields
//   come from the schema (their descriptor's value), never from the wire.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>
#include <type_traits>

#include "b3/walker.hpp"
#include "b3/wire.hpp"

namespace b3::decode {

// A compile-time list of descriptor types (fields.hpp).
template <class... Ts>
struct List {};

// How a field's bytes become a value.
enum class Kind : std::uint8_t {
    integer,     // integers, enums, sets, decimals (mantissa), timestamps
    character,   // char
    char_array,  // char[N]: ASCII, padded with NUL on the right
    composite,   // several members (MaturityMonthYear): see members
};

enum class Presence : std::uint8_t { absent, null, present };

template <class T>
struct Value {
    Presence presence = Presence::absent;
    T value{};
    constexpr bool absent() const noexcept { return presence == Presence::absent; }
    constexpr bool null() const noexcept { return presence == Presence::null; }
    constexpr bool present() const noexcept { return presence == Presence::present; }
};

// A block of fixed fields: the root block, a group entry, or a composite field.
struct Block {
    const std::uint8_t* data;
    std::uint16_t length;    // bytes of the block the wire gives (blockLength)
    std::uint16_t version;   // the message header's schema version
};

inline Block root(const walker::MessageView& m) noexcept {
    return {m.body, m.block_length, m.version};
}

// The type get<F> returns a value in.
template <class F>
using value_type_t = std::conditional_t<F::kind == Kind::char_array, std::string_view,
                                        typename F::type>;

template <class F>
constexpr bool exists(const Block& b) noexcept {
    return F::since_version <= b.version &&
           static_cast<std::size_t>(F::offset) + F::size <= b.length;
}

// The accessor for field F of a block.
template <class F>
inline Value<value_type_t<F>> get(const Block& b) noexcept {
    static_assert(F::kind != Kind::composite, "read a composite through its members");
    Value<value_type_t<F>> out;
    if (!exists<F>(b)) {
        return out;   // absent
    }
    const std::uint8_t* p = b.data + F::offset;
    bool is_null = false;
    if constexpr (F::kind == Kind::char_array) {
        std::size_t n = F::size;
        while (n > 0 && p[n - 1] == 0) --n;          // NUL padding on the right
        out.value = std::string_view(reinterpret_cast<const char*>(p), n);
        if constexpr (F::optional) is_null = (n == 0);   // char[N] null: all NUL
    } else if constexpr (F::kind == Kind::character) {
        out.value = static_cast<char>(p[0]);
        if constexpr (F::optional) is_null = (out.value == F::null_value);
    } else {
        out.value = wire::load_le<typename F::type>(p);
        if constexpr (F::optional) is_null = (out.value == F::null_value);
    }
    out.presence = is_null ? Presence::null : Presence::present;
    return out;
}

// The accessor for root-block field F of a message.
template <class F>
inline Value<value_type_t<F>> get(const walker::MessageView& m) noexcept {
    return get<F>(root(m));
}

// The block holding a composite field's members (offsets relative to the field).
template <class F>
inline Block composite_block(const Block& b) noexcept {
    static_assert(F::kind == Kind::composite);
    return {b.data + F::offset, F::size, b.version};
}

// Field Name of a descriptor list, read as an unsigned integer (absent if the
// list has no such field). Which descriptor is read is resolved at compile
// time: Name must be a constant (a static constexpr std::string_view).
template <const std::string_view& Name, class... Fs>
inline Value<std::uint64_t> get_named(List<Fs...>, const Block& b) noexcept {
    Value<std::uint64_t> out;
    (
        [&] {
            if constexpr (Fs::name == Name) {
                const auto v = get<Fs>(b);
                out = {v.presence, static_cast<std::uint64_t>(v.value)};
            }
        }(),
        ...);
    return out;
}

template <const std::string_view& Name, class... Fs>
constexpr bool has_named(List<Fs...>) noexcept {
    return ((Fs::name == Name) || ...);
}

// A constant field: the schema's value, never the wire's.
template <class C>
constexpr auto constant() noexcept {
    return C::value;
}

// ---- Groups and var data ----

// Why a body walk stopped (DESIGN.md 3.6.2), in the order the walk meets them.
enum class BodyFault : std::uint8_t {
    group_header_past_end,     // the group's dimension header does not fit
    group_entry_past_end,      // an entry of the wire's blockLength does not fit
    var_data_length_past_end,  // a var data length prefix does not fit
    var_data_past_end,         // the var data bytes do not fit
};
inline constexpr std::size_t kBodyFaults = 4;
inline constexpr std::array<std::string_view, kBodyFaults> kBodyFaultNames = {
    "group_header_past_end", "group_entry_past_end", "var_data_length_past_end",
    "var_data_past_end",
};

namespace detail {

template <class... Ts, class Fn>
constexpr bool all_of(List<Ts...>, Fn&& fn) {
    return (fn.template operator()<Ts>() && ...);
}

}  // namespace detail

// Walks the groups, then the var data, of template T (a fields::Template<Id>)
// in message m. The visitor gets, in wire order:
//   on_group<G>(entry_count, entry_block_length)
//   on_entry<G>(index, Block)              each entry that fits
//   on_var_data<D>(std::string_view bytes)
//   on_body_fault<Where>(BodyFault)        once, at the first read that does not fit
// Returns false if a body fault stopped the walk.
template <class T, class Visitor>
inline bool walk_body(const walker::MessageView& m, Visitor& v) {
    const std::size_t end = m.message_length;   // offsets from the message start
    std::size_t pos = walker::mh::size + static_cast<std::size_t>(m.block_length);
    const std::uint8_t* msg = m.message;

    const bool groups_ok = detail::all_of(typename T::groups{}, [&]<class G>() {
        if (end - pos < G::dimension_size) {
            v.template on_body_fault<G>(BodyFault::group_header_past_end);
            return false;
        }
        const auto entry_length = wire::load_le<typename G::block_length_type>(msg + pos + G::block_length_offset);
        const auto count = wire::load_le<typename G::num_in_group_type>(msg + pos + G::num_in_group_offset);
        pos += G::dimension_size;
        v.template on_group<G>(count, entry_length);
        for (std::size_t i = 0; i < count; ++i) {
            if (end - pos < entry_length) {
                v.template on_body_fault<G>(BodyFault::group_entry_past_end);
                return false;
            }
            v.template on_entry<G>(i, Block{msg + pos, entry_length, m.version});
            pos += entry_length;
        }
        return true;
    });
    if (!groups_ok) return false;

    return detail::all_of(typename T::var_data{}, [&]<class D>() {
        if (end - pos < D::prefix_size) {
            v.template on_body_fault<D>(BodyFault::var_data_length_past_end);
            return false;
        }
        const std::size_t length = wire::load_le<typename D::prefix_type>(msg + pos);
        pos += D::prefix_size;
        if (end - pos < length) {
            v.template on_body_fault<D>(BodyFault::var_data_past_end);
            return false;
        }
        v.template on_var_data<D>(std::string_view(reinterpret_cast<const char*>(msg + pos), length));
        pos += length;
        return true;
    });
    // Bytes after the last var data, inside messageLength, are skipped.
}

}  // namespace b3::decode
