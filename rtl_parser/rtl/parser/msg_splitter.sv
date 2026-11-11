// msg_splitter: cuts a UDP payload into B3 messages, realigned to lane 0.
//
// 1. Role
//    In:  one B3 packet per tlast (16-byte packet header + messages).
//    Out: one message per tlast, its byte 0 in lane 0 of its first beat,
//         12-byte message header included. Every message is passed on;
//         sbe_decoder ignores templates it doesn't decode.
//    Sideband (constant for the whole message): channel, seq_version,
//    seq_num, msg_index. m_msg_ok on the tlast beat: 1 = complete message.
//
// 2. How: a 16-byte holding buffer
//    Input beats are appended at the back of buf_q, output beats are taken
//    from the front. Taking from the front is what realigns: a message that
//    started in lane 4 of an input beat starts at buf_q[0] once the bytes
//    before it have been taken.
//
// 3. Malformed packets (docs/DESIGN.md section 4)
//    - datagram < 16 bytes: dropped
//    - messageLength < 12, encodingType != 0xEB50,
//      blockLength > messageLength - 12: rest of packet dropped
//    - packet ends before the message does: message closed with
//      m_msg_ok = 0 (that last beat may carry 0 bytes, tkeep = 0)
//    - packet ends inside the first 6 header bytes: dropped
//
// 4. One packet at a time: the next packet is accepted only after the
//    current one is fully emptied from the buffer.

module msg_splitter
    import pkg_types::*;
(
    input  logic        clk,
    input  logic        rst_n,

    // ---- 1. In: UDP payload ----
    input  logic [63:0] s_axis_tdata,
    input  logic [7:0]  s_axis_tkeep,
    input  logic        s_axis_tvalid,
    output logic        s_axis_tready,
    input  logic        s_axis_tlast,

    // ---- 2. Out: one message per tlast ----
    output logic [63:0] m_axis_tdata,
    output logic [7:0]  m_axis_tkeep,
    output logic        m_axis_tvalid,
    input  logic        m_axis_tready,
    output logic        m_axis_tlast,

    // ---- 3. Out: sideband ----
    output logic [7:0]  m_channel,
    output logic [15:0] m_seq_version,
    output logic [31:0] m_seq_num,
    output logic [7:0]  m_msg_index,
    output logic        m_msg_ok
);

    // ---- 1. State ----
    typedef enum logic [1:0] {
        S_PKT_HDR,    // waiting for the 16-byte packet header
        S_MSG_HDR,    // at the start of a message: check its header
        S_MSG_BODY,   // emitting the rest of a message
        S_DROP        // discarding the rest of a malformed packet
    } state_t;

    state_t      state_q;
    logic [7:0]  buf_q [16];   // buf_q[0] = next byte of the stream
    logic [4:0]  cnt_q;        // bytes held, 0..16
    logic        last_in_q;    // the packet's tlast beat is in the buffer
    logic [15:0] rem_q;        // bytes of the current message still to emit
    logic [7:0]  channel_q;
    logic [15:0] seq_version_q;
    logic [31:0] seq_num_q;
    logic [7:0]  msg_index_q;

    // ---- 2. Message header fields, read at the front of the buffer ----
    logic [15:0] h_len;   // messageLength (includes the 12-byte header)
    logic [15:0] h_enc;   // encodingType
    logic [15:0] h_bl;    // blockLength
    assign h_len = {buf_q[1], buf_q[0]};
    assign h_enc = {buf_q[3], buf_q[2]};
    assign h_bl  = {buf_q[5], buf_q[4]};

    logic hdr_bad;
    assign hdr_bad = (h_len < 16'd12)
                  || (h_enc != SBE_ENCODING)
                  || (h_bl > h_len - 16'd12);

    // ---- 3. Per-cycle decisions ----
    logic [4:0]  take;        // bytes removed from the front this cycle
    logic        emit;        // an output beat is offered (m_axis_tvalid)
    logic [3:0]  out_n;       // bytes in the offered beat, 0..8
    logic        complete;    // offered beat ends the message normally
    logic        truncated;   // offered beat ends the message early (packet ended)
    logic [15:0] rem_eff;     // bytes left in the message, counting this beat
    logic [15:0] want;        // min(8, rem_eff)
    logic        hdr_capture; // packet header taken this cycle
    logic        pkt_done;    // packet fully handled this cycle
    state_t      state_d;
    logic        fire;        // output handshake
    logic        accept;      // input handshake
    logic [3:0]  in_n;        // bytes in the input beat

    assign fire   = emit && m_axis_tready;
    assign in_n   = 4'($countones(s_axis_tkeep));
    assign accept = s_axis_tvalid && s_axis_tready;

    // Room for 8 more bytes after this cycle's take.
    assign s_axis_tready = !last_in_q && ((cnt_q - take) <= 5'd8);

    always_comb begin
        take        = '0;
        emit        = 1'b0;
        out_n       = '0;
        complete    = 1'b0;
        truncated   = 1'b0;
        hdr_capture = 1'b0;
        pkt_done    = 1'b0;
        state_d     = state_q;

        rem_eff = (state_q == S_MSG_HDR) ? h_len : rem_q;
        want    = (rem_eff < 16'd8) ? rem_eff : 16'd8;

        case (state_q)

            // 3a. Packet header: take all 16 bytes at once
            S_PKT_HDR: begin
                if (cnt_q == 5'd16) begin
                    take        = 5'd16;
                    hdr_capture = 1'b1;
                    state_d     = S_MSG_HDR;
                end else if (last_in_q) begin
                    // Datagram shorter than 16 bytes
                    take     = cnt_q;
                    pkt_done = 1'b1;
                end
            end

            // 3b. Message start: packet over, header cut, header bad, or start emitting
            S_MSG_HDR: begin
                if (last_in_q && cnt_q < 5'd6) begin
                    // Packet ended cleanly (0 bytes) or inside the header's first 6 bytes
                    take     = cnt_q;
                    pkt_done = 1'b1;
                    state_d  = S_PKT_HDR;
                end else if (cnt_q >= 5'd8 || last_in_q) begin
                    if (hdr_bad) begin
                        take    = cnt_q;
                        state_d = S_DROP;
                    end else begin
                        emit = 1'b1;
                    end
                end
            end

            // 3c. Message body
            S_MSG_BODY: emit = 1'b1;

            // 3d. Discard everything until the packet's tlast has been taken
            S_DROP: begin
                take = cnt_q;
                if (last_in_q) begin
                    pkt_done = 1'b1;
                    state_d  = S_PKT_HDR;
                end
            end

            default: ;
        endcase

        // 3e. Shape the offered beat: full `want` bytes, or what's left if the packet ended
        if (emit) begin
            if ({11'd0, cnt_q} >= want) begin
                out_n    = 4'(want);
                complete = (want == rem_eff);
            end else if (last_in_q) begin
                out_n     = 4'(cnt_q);
                truncated = 1'b1;
            end else begin
                emit = 1'b0;   // wait for more input bytes
            end
        end

        // 3f. On handshake: remove the bytes, move on
        if (emit && m_axis_tready) begin
            take = 5'(out_n);
            if (complete) begin
                state_d = S_MSG_HDR;
            end else if (truncated) begin
                pkt_done = 1'b1;
                state_d  = S_PKT_HDR;
            end else begin
                state_d = S_MSG_BODY;
            end
        end
    end

    // ---- 4. Output beat = front 8 bytes of the buffer ----
    always_comb begin
        for (int i = 0; i < 8; i++) begin
            m_axis_tdata[8*i +: 8] = buf_q[i];
        end
    end

    assign m_axis_tvalid = emit;
    assign m_axis_tkeep  = 8'((9'd1 << out_n) - 9'd1);
    assign m_axis_tlast  = complete || truncated;
    assign m_msg_ok      = complete;
    assign m_channel     = channel_q;
    assign m_seq_version = seq_version_q;
    assign m_seq_num     = seq_num_q;
    assign m_msg_index   = msg_index_q;

    // ---- 5. Next buffer: shift out `take` bytes, append the input beat ----
    logic [7:0] buf_d [16];
    logic [4:0] base;   // where the input beat lands after the shift
    assign base = cnt_q - take;

    always_comb begin
        for (int i = 0; i < 16; i++) begin
            buf_d[i] = (i + int'(take) < 16) ? buf_q[i + int'(take)] : 8'h00;
        end
        if (accept) begin
            for (int i = 0; i < 16; i++) begin
                if (i >= int'(base) && i < int'(base) + int'(in_n)) begin
                    buf_d[i] = s_axis_tdata[8*(i - int'(base)) +: 8];
                end
            end
        end
    end

    // ---- 6. Registers ----
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            state_q     <= S_PKT_HDR;
            cnt_q       <= '0;
            last_in_q   <= 1'b0;
            rem_q       <= '0;
            msg_index_q <= '0;
        end else begin
            state_q <= state_d;
            buf_q   <= buf_d;
            cnt_q   <= base + (accept ? 5'(in_n) : 5'd0);

            // 6a. Packet boundary
            if (pkt_done) begin
                last_in_q <= 1'b0;
            end else if (accept && s_axis_tlast) begin
                last_in_q <= 1'b1;
            end

            // 6b. Packet header -> sideband
            if (hdr_capture) begin
                channel_q     <= buf_q[0];
                seq_version_q <= {buf_q[3], buf_q[2]};
                seq_num_q     <= {buf_q[7], buf_q[6], buf_q[5], buf_q[4]};
                msg_index_q   <= '0;
            end

            // 6c. Message progress
            if (fire) begin
                rem_q <= rem_eff - 16'(out_n);
                if (complete) begin
                    msg_index_q <= msg_index_q + 8'd1;
                end
            end
        end
    end

endmodule
