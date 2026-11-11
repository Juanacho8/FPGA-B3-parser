// pkg_types_tb: test-only wrapper. A package has no ports, so this module
// takes each record field as an input and outputs the packed decoded_msg_t.
// cocotb compares `rec` with tb/lib/record.py pack().

module pkg_types_tb
    import pkg_types::*;
(
    // ---- 1. Context ----
    input  logic [7:0]  channel,
    input  logic [15:0] seq_version,
    input  logic [31:0] seq_num,
    input  logic [7:0]  msg_index,
    input  logic [15:0] template_id,
    input  logic [15:0] schema_version,

    // ---- 2. Absent mask ----
    input  logic [11:0] absent,

    // ---- 3. Fields ----
    input  logic [63:0] security_id,
    input  logic [7:0]  match_event_ind,
    input  logic [7:0]  md_update_action,
    input  logic [7:0]  md_entry_type,
    input  logic [63:0] md_entry_px,
    input  logic [63:0] md_entry_size,
    input  logic [31:0] entering_firm,
    input  logic [63:0] md_insert_ts,
    input  logic [63:0] secondary_order_id,
    input  logic [31:0] rpt_seq,
    input  logic [63:0] transact_time,
    input  logic [63:0] md_entry_prev_size,

    // ---- 4. Outputs ----
    output logic [$bits(decoded_msg_t)-1:0] rec,
    output logic [15:0]                     rec_bits
);

    decoded_msg_t r;

    // ---- 5. Fill the struct by name ----
    always_comb begin
        r.ctx.channel          = channel;
        r.ctx.seq_version      = seq_version;
        r.ctx.seq_num          = seq_num;
        r.ctx.msg_index        = msg_index;
        r.ctx.template_id      = template_id;
        r.ctx.schema_version   = schema_version;
        r.absent               = absent;
        r.f.security_id        = security_id;
        r.f.match_event_ind    = match_event_ind;
        r.f.md_update_action   = md_update_action;
        r.f.md_entry_type      = md_entry_type;
        r.f.md_entry_px        = md_entry_px;
        r.f.md_entry_size      = md_entry_size;
        r.f.entering_firm      = entering_firm;
        r.f.md_insert_ts       = md_insert_ts;
        r.f.secondary_order_id = secondary_order_id;
        r.f.rpt_seq            = rpt_seq;
        r.f.transact_time      = transact_time;
        r.f.md_entry_prev_size = md_entry_prev_size;
    end

    // ---- 6. Flatten ----
    assign rec      = r;
    assign rec_bits = 16'($bits(decoded_msg_t));

endmodule
