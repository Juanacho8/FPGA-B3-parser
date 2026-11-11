// pkg_types: shared constants and the decoded-message record.
//
// 1. Role
//    One definition used by msg_splitter, sbe_decoder, later stages, and
//    (mirrored in tb/lib/record.py) the cocotb tests.
//
// 2. Record = context (96) + absent mask (12) + fields (536) = 644 bits.
//    Packed structs put the FIRST member in the HIGHEST bits:
//    channel is [643:636], md_entry_prev_size is [63:0].
//
// 3. Field rules
//    - Fields are raw wire bits (little-endian slices, no swapping).
//    - Null values keep their wire encoding (no extra flag).
//    - absent[i] = 1: field i is not on the wire for this message
//      (not in the template, or offset+size > blockLength). Value is 0.
//    - Constant fields (not on the wire) are not stored; templateId implies them.

package pkg_types;

    // ---- 1. Byte stream (AXI-Stream) ----
    localparam int AXIS_DATA_W = 64;
    localparam int AXIS_KEEP_W = 8;

    // ---- 2. Framing (spec/LAYOUTS.md sections 3-4) ----
    localparam int          PKT_HDR_BYTES = 16;
    localparam int          MSG_HDR_BYTES = 12;
    localparam logic [15:0] SBE_ENCODING  = 16'hEB50;

    // ---- 3. Decoded templates (spec/LAYOUTS.md sections 6-7) ----
    localparam logic [15:0] TPL_ORDER_MBO              = 16'd50;  // blockLength 72
    localparam logic [15:0] TPL_DELETE_ORDER_MBO       = 16'd51;  // blockLength 52
    localparam logic [15:0] TPL_MASS_DELETE_ORDERS_MBO = 16'd52;  // blockLength 28

    // ---- 4. Absent-mask bit per field (Order_MBO_50 wire order) ----
    localparam int ABS_SECURITY_ID        = 0;
    localparam int ABS_MATCH_EVENT_IND    = 1;
    localparam int ABS_MD_UPDATE_ACTION   = 2;
    localparam int ABS_MD_ENTRY_TYPE      = 3;
    localparam int ABS_MD_ENTRY_PX        = 4;
    localparam int ABS_MD_ENTRY_SIZE      = 5;
    localparam int ABS_ENTERING_FIRM      = 6;
    localparam int ABS_MD_INSERT_TS       = 7;
    localparam int ABS_SECONDARY_ORDER_ID = 8;
    localparam int ABS_RPT_SEQ            = 9;
    localparam int ABS_TRANSACT_TIME      = 10;
    localparam int ABS_MD_ENTRY_PREV_SIZE = 11;

    // ---- 5. Message context: where the message came from (96 bits) ----
    typedef struct packed {
        logic [7:0]  channel;          // packet header byte 0
        logic [15:0] seq_version;      // packet header bytes 2-3
        logic [31:0] seq_num;          // packet header bytes 4-7
        logic [7:0]  msg_index;        // position in packet, skipped messages included
        logic [15:0] template_id;      // message header bytes 6-7
        logic [15:0] schema_version;   // message header bytes 10-11
    } msg_ctx_t;

    // ---- 6. Fields: the 12 Order_MBO_50 wire fields, a superset of 51/52 (536 bits) ----
    typedef struct packed {
        logic [63:0] security_id;
        logic [7:0]  match_event_ind;
        logic [7:0]  md_update_action;
        logic [7:0]  md_entry_type;       // ASCII char
        logic [63:0] md_entry_px;         // int64 mantissa x 10^-4
        logic [63:0] md_entry_size;       // int64
        logic [31:0] entering_firm;
        logic [63:0] md_insert_ts;        // ns since epoch
        logic [63:0] secondary_order_id;
        logic [31:0] rpt_seq;
        logic [63:0] transact_time;       // ns since epoch
        logic [63:0] md_entry_prev_size;  // int64
    } mbo_fields_t;

    // ---- 7. The record ----
    typedef struct packed {
        msg_ctx_t    ctx;
        logic [11:0] absent;
        mbo_fields_t f;
    } decoded_msg_t;

endpackage
