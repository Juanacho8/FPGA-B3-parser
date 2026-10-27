// instruments.cpp: loading the instrument table (see include/b3/instruments.hpp).
//
// What it does:
//   Parses "securityID channel" lines, '#' comments and blank lines, and
//   rejects malformed lines, channels above 255, duplicate securityIDs and
//   more entries than MAX_INSTRUMENTS, naming the line.
//
// What it must not do:
//   Run in timed code. Loading happens once, at startup.

#include "b3/instruments.hpp"

#include <charconv>
#include <fstream>
#include <sstream>
#include <string_view>

namespace b3::instruments {

namespace {

std::string_view trim(std::string_view s) {
    while (!s.empty() && (s.front() == ' ' || s.front() == '\t' || s.front() == '\r')) s.remove_prefix(1);
    while (!s.empty() && (s.back() == ' ' || s.back() == '\t' || s.back() == '\r')) s.remove_suffix(1);
    return s;
}

// Reads one unsigned integer token from s (advancing it); false if none.
bool read_uint(std::string_view& s, std::uint64_t& out) {
    s = trim(s);
    std::size_t n = 0;
    while (n < s.size() && s[n] != ' ' && s[n] != '\t') ++n;
    if (n == 0) return false;
    const auto token = s.substr(0, n);
    const auto [end, ec] = std::from_chars(token.data(), token.data() + token.size(), out);
    if (ec != std::errc() || end != token.data() + token.size()) return false;
    s.remove_prefix(n);
    return true;
}

}  // namespace

Table Table::load(const std::filesystem::path& path) {
    std::ifstream in(path);
    if (!in) throw LoadError(path.string() + ": cannot open");
    std::stringstream ss;
    ss << in.rdbuf();
    return parse(ss.str(), path.string());
}

Table Table::parse(const std::string& text, const std::string& where) {
    Table t;
    std::istringstream lines(text);
    std::string raw;
    for (int line_no = 1; std::getline(lines, raw); ++line_no) {
        std::string_view line = raw;
        if (const auto hash = line.find('#'); hash != std::string_view::npos) line = line.substr(0, hash);
        line = trim(line);
        if (line.empty()) continue;
        const std::string at = where + ":" + std::to_string(line_no) + ": ";
        std::uint64_t sid = 0, channel = 0;
        if (!read_uint(line, sid) || !read_uint(line, channel) || !trim(line).empty()) {
            throw LoadError(at + "expected 'securityID channel', got '" + raw + "'");
        }
        if (channel > 255) throw LoadError(at + "channel " + std::to_string(channel) + " is above 255");
        if (t.find(sid) != kNotFound) {
            throw LoadError(at + "securityID " + std::to_string(sid) + " is already in the table");
        }
        if (t.count_ == kMaxInstruments) {
            throw LoadError(at + "more than MAX_INSTRUMENTS (" + std::to_string(kMaxInstruments) +
                            ") instruments");
        }
        t.entries_[t.count_++] = Entry{sid, static_cast<std::uint8_t>(channel)};
    }
    return t;
}

}  // namespace b3::instruments
