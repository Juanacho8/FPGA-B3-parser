// reset_sync: asynchronous assert, synchronous release.
//
// 1. Role
//    Turns a raw async reset (button, PLL lock) into a reset that is safe to
//    use in the clk domain. Assert happens immediately; release happens only
//    on a clk edge, after STAGES edges.
//
// 2. Why a chain of flops
//    The first flop may go metastable if arst_n releases near a clk edge.
//    Each extra flop gives it one more clock period to settle before
//    anything downstream sees it.

module reset_sync #(
    parameter int STAGES = 2
) (
    input  logic clk,
    input  logic arst_n,   // raw async reset, active-low
    output logic rst_n     // synchronized reset, active-low
);

    // ---- 1. Synchronizer chain ----
    // ASYNC_REG tells Vivado: keep these flops adjacent, treat as synchronizer.
    (* ASYNC_REG = "TRUE" *) logic [STAGES-1:0] sync_q;

    // ---- 2. Assert async, release by shifting 1s in ----
    always_ff @(posedge clk or negedge arst_n) begin
        if (!arst_n) begin
            sync_q <= '0;
        end else begin
            sync_q <= {sync_q[STAGES-2:0], 1'b1};
        end
    end

    // ---- 3. Output is the last stage ----
    assign rst_n = sync_q[STAGES-1];

endmodule
