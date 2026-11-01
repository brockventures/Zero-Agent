// ============================================================================
// GOTCHI Circular Log Ring Buffer (F6 / T2.2) Implementation
// ============================================================================

#include "log_buffer.h"
#include <esp_heap_caps.h>
#include <string.h>
#include <strings.h>
#include <stdio.h>

namespace gotchi_log {

LogBuffer RingLogger(DEFAULT_RING_CAPACITY);

static const char* levelToString(Level level) {
    switch (level) {
        case Level::DEBUG: return "DEBUG";
        case Level::INFO:  return "INFO";
        case Level::WARN:  return "WARN";
        case Level::ERROR: return "ERROR";
        default:           return "INFO";
    }
}

static void replaceAllSubstrings(char* buf, size_t cap, const char* needle, const char* mask) {
    if (!needle || !*needle || !mask) return;
    size_t needle_len = strlen(needle);
    size_t mask_len = strlen(mask);
    char* search_from = buf;
    char* pos = nullptr;
    while ((pos = strstr(search_from, needle)) != nullptr) {
        size_t tail_len = strlen(pos + needle_len);
        size_t prefix_len = (size_t)(pos - buf);
        if (prefix_len + mask_len + tail_len < cap) {
            memmove(pos + mask_len, pos + needle_len, tail_len + 1);
            memcpy(pos, mask, mask_len);
            search_from = pos + mask_len;
        } else {
            // Truncate cleanly at mask boundary
            size_t avail = cap - prefix_len - 1;
            if (avail > 0) {
                strncpy(pos, mask, avail);
            }
            buf[cap - 1] = '\0';
            break;
        }
    }
}

static void redactPatternValues(char* buf, size_t cap, const char* prefix, const char* mask) {
    size_t prefix_len = strlen(prefix);
    size_t mask_len = strlen(mask);
    char* search_from = buf;
    while (*search_from) {
        char* found = nullptr;
        for (char* p = search_from; *p; ++p) {
            if (strncasecmp(p, prefix, prefix_len) == 0) {
                found = p;
                break;
            }
        }
        if (!found) break;

        char* val_start = found + prefix_len;
        while (*val_start == ' ') val_start++;
        bool quoted = (*val_start == '"' || *val_start == '\'');
        char quote_char = quoted ? *val_start++ : '\0';

        char* val_end = val_start;
        if (quoted) {
            while (*val_end && *val_end != quote_char && *val_end != '\r' && *val_end != '\n') {
                val_end++;
            }
        } else {
            while (*val_end && *val_end != ' ' && *val_end != '"' && *val_end != '\'' &&
                   *val_end != ',' && *val_end != ';' && *val_end != '\r' && *val_end != '\n') {
                val_end++;
            }
        }

        if (val_end > val_start) {
            size_t tail_len = strlen(val_end);
            size_t prefix_offset = (size_t)(val_start - buf);
            if (prefix_offset + mask_len + tail_len < cap) {
                memmove(val_start + mask_len, val_end, tail_len + 1);
                memcpy(val_start, mask, mask_len);
                search_from = val_start + mask_len;
            } else {
                size_t avail = cap - prefix_offset - 1;
                if (avail > 0) strncpy(val_start, mask, avail);
                buf[cap - 1] = '\0';
                break;
            }
        } else {
            search_from = val_start;
        }
    }
}

void scrubCredentials(char* out, size_t out_size, const char* in, const char* psk, const char* token) {
    if (!out || out_size == 0) return;
    if (!in) {
        out[0] = '\0';
        return;
    }

    char temp[MAX_MSG_LEN * 2];
    strncpy(temp, in, sizeof(temp) - 1);
    temp[sizeof(temp) - 1] = '\0';

    // 1. Redact explicit PSK regardless of length (any non-empty PSK)
    if (psk && *psk) {
        replaceAllSubstrings(temp, sizeof(temp), psk, "[REDACTED_PSK]");
    }

    // 2. Redact explicit Bearer token regardless of length (any non-empty token)
    if (token && *token) {
        replaceAllSubstrings(temp, sizeof(temp), token, "[REDACTED_TOKEN]");
    }

    // 3. Redact all occurrences of generic "Bearer <token>", "psk=<psk>", "password=<pw>", "token=<tok>"
    redactPatternValues(temp, sizeof(temp), "Bearer ", "[REDACTED_TOKEN]");
    redactPatternValues(temp, sizeof(temp), "psk=", "[REDACTED_PSK]");
    redactPatternValues(temp, sizeof(temp), "password=", "[REDACTED_PSK]");
    redactPatternValues(temp, sizeof(temp), "token=", "[REDACTED_TOKEN]");

    strncpy(out, temp, out_size - 1);
    out[out_size - 1] = '\0';
}

LogBuffer::LogBuffer(size_t capacity)
    : capacity_(capacity > 0 ? capacity : DEFAULT_RING_CAPACITY) {}

LogBuffer::~LogBuffer() {
    if (entries_) {
        free(entries_);
        entries_ = nullptr;
    }
}

bool LogBuffer::init(bool prefer_psram) {
    portENTER_CRITICAL(&mux_);
    if (entries_) {
        portEXIT_CRITICAL(&mux_);
        return true;
    }
    portEXIT_CRITICAL(&mux_);

    size_t alloc_bytes = capacity_ * sizeof(LogEntry);
    LogEntry* allocated = nullptr;
    bool psram = false;

    if (prefer_psram && psramFound()) {
        allocated = static_cast<LogEntry*>(heap_caps_malloc(alloc_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (allocated) {
            psram = true;
        }
    }

    if (!allocated) {
        allocated = static_cast<LogEntry*>(malloc(alloc_bytes));
        psram = false;
    }

    if (!allocated) {
        size_t half = capacity_ / 2;
        if (half > 0) {
            allocated = static_cast<LogEntry*>(malloc(half * sizeof(LogEntry)));
            if (allocated) capacity_ = half;
        }
    }

    if (!allocated) return false;
    memset(allocated, 0, capacity_ * sizeof(LogEntry));

    portENTER_CRITICAL(&mux_);
    if (!entries_) {
        entries_ = allocated;
        in_psram_ = psram;
        head_ = 0;
        count_ = 0;
        total_logged_ = 0;
        portEXIT_CRITICAL(&mux_);
        return true;
    }
    portEXIT_CRITICAL(&mux_);
    free(allocated);
    return true;
}

void LogBuffer::clear() {
    portENTER_CRITICAL(&mux_);
    head_ = 0;
    count_ = 0;
    if (entries_) {
        memset(entries_, 0, capacity_ * sizeof(LogEntry));
    }
    portEXIT_CRITICAL(&mux_);
}

uint32_t LogBuffer::totalLogged() const {
    portENTER_CRITICAL(&mux_);
    uint32_t total = total_logged_;
    portEXIT_CRITICAL(&mux_);
    return total;
}

size_t LogBuffer::count() const {
    portENTER_CRITICAL(&mux_);
    size_t c = count_;
    portEXIT_CRITICAL(&mux_);
    return c;
}

void LogBuffer::log(Level level, const char* tag, const char* msg, const char* psk, const char* token) {
    if (!entries_) {
        if (!init(true)) return;
    }

    // Format and scrub outside critical section to keep spinlock hold time minimal
    uint32_t ts = millis();
    char clean_tag[MAX_TAG_LEN];
    if (tag && *tag) {
        strncpy(clean_tag, tag, sizeof(clean_tag) - 1);
        clean_tag[sizeof(clean_tag) - 1] = '\0';
    } else {
        strncpy(clean_tag, "sys", sizeof(clean_tag) - 1);
        clean_tag[sizeof(clean_tag) - 1] = '\0';
    }

    char clean_msg[MAX_MSG_LEN];
    scrubCredentials(clean_msg, sizeof(clean_msg), msg, psk, token);

    portENTER_CRITICAL(&mux_);
    LogEntry& entry = entries_[head_];
    entry.timestamp_ms = ts;
    entry.level = level;
    memcpy(entry.tag, clean_tag, sizeof(entry.tag));
    memcpy(entry.message, clean_msg, sizeof(entry.message));

    head_ = (head_ + 1) % capacity_;
    if (count_ < capacity_) {
        count_++;
    }
    total_logged_++;
    portEXIT_CRITICAL(&mux_);
    // Note: Intentionally never writes to `Serial` so USB-C CDC JSON-RPC stream stays uncorrupted (same invariant as GxEPD2 init(0)).
}

void LogBuffer::dumpJson(JsonArray& array, size_t limit, const char* tag_filter, Level min_level) const {
    if (!entries_) return;

    // Take a consistent snapshot of matching entries under lock
    size_t cap = capacity_;
    LogEntry* snapshot = static_cast<LogEntry*>(malloc(cap * sizeof(LogEntry)));
    if (!snapshot) return;

    size_t cur_count = 0;
    size_t cur_head = 0;

    portENTER_CRITICAL(&mux_);
    cur_count = count_;
    cur_head = head_;
    if (cur_count > 0) {
        memcpy(snapshot, entries_, cap * sizeof(LogEntry));
    }
    portEXIT_CRITICAL(&mux_);

    if (cur_count == 0) {
        free(snapshot);
        return;
    }

    if (limit == 0 || limit > cur_count) {
        limit = cur_count;
    }

    // Walk newest-to-oldest across ALL `cur_count` entries, collecting up to `limit` matches,
    // then emit them in chronological order (oldest to newest).
    size_t* matched_indices = static_cast<size_t*>(malloc(limit * sizeof(size_t)));
    if (!matched_indices) {
        free(snapshot);
        return;
    }

    size_t matched_count = 0;
    const bool has_tag_filter = (tag_filter != nullptr && strlen(tag_filter) > 0);

    for (size_t i = 0; i < cur_count && matched_count < limit; ++i) {
        // Newest entry is at (cur_head - 1 - i) mod cap
        size_t idx = (cur_head + cap - 1 - (i % cap)) % cap;
        const LogEntry& entry = snapshot[idx];

        if (entry.level < min_level) continue;
        if (has_tag_filter && strcasecmp(entry.tag, tag_filter) != 0) continue;

        matched_indices[matched_count++] = idx;
    }

    // Emit in chronological order (reverse of newest-to-oldest collection)
    for (size_t i = matched_count; i > 0; --i) {
        const LogEntry& entry = snapshot[matched_indices[i - 1]];
        JsonObject obj = array.add<JsonObject>();
        obj["ts"] = entry.timestamp_ms;
        obj["lvl"] = levelToString(entry.level);
        obj["tag"] = entry.tag;
        obj["msg"] = entry.message;
    }

    free(matched_indices);
    free(snapshot);
}

} // namespace gotchi_log
