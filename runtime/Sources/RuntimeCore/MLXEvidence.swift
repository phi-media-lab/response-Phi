import Foundation

func validateMLXCommon(_ frame:JSONValue,kind:String,session:MLXSessionIdentity)throws {
    try runtimeRequire(frame.field("v") == .integer(1) && frame.field("kind").text() == kind &&
        frame.field("session").text() == session.session && frame.field("worker_id").text() == session.worker &&
        backendInt(frame.field("pid")) == UInt64(session.pid) && backendInt(frame.field("pgid")) == UInt64(session.pgid),"foreign MLX worker frame")
    try runtimeRequire(backendInt(frame.field("event_seq")) > 0 && backendInt(frame.field("worker_ns")) > 0,"invalid worker event identity")
}

func validateMLXPool(_ row:JSONValue,pool:MLXPoolIdentity)throws {
    _ = try row.members(exactly:["layer","pool_id","pool_object_id","cache_page_ids","page_lens","offset","owned_pages","used_page_ids","free_page_ids","num_pages","capacity","storage_nbytes"])
    try runtimeRequire(backendInt(row.field("layer")) == UInt64(pool.layer) && row.field("pool_id").text() == pool.poolID && row.field("pool_object_id").text() == pool.objectID,"foreign layer/pool")
    let used=try backendInts(row.field("used_page_ids")),free=try backendInts(row.field("free_page_ids"))
    let count=try backendInt(row.field("num_pages")),capacity=try backendInt(row.field("capacity"))
    try runtimeRequire(count <= 1 && capacity >= count && capacity <= 1024,"CM1 logical pool geometry")
    try runtimeRequire(Set(used).count == used.count && Set(free).count == free.count && Set(used).isDisjoint(with:Set(free)) &&
        Set(used+free) == Set((0..<count).map{$0}),"pool partition is not conserved")
    let bytes=try backendInt(row.field("storage_nbytes"))
    try runtimeRequire(count == 0 ? bytes == 0 : bytes > 0,"pool backing byte evidence")
}

/// Returns a proposed page history. Caller commits it only after every guard
/// passes. Source epoch, wire lease, and actual per-page allocation epoch never
/// share an integer-equality invariant.
func validateMLXReceipt(_ receipt:MLXHostReceipt,expected:MLXEvalRequest,lastSequence:UInt64,lastLease:UInt64,pageHistory:[String:UInt64])throws->[String:UInt64] {
    try runtimeRequire(receipt.request == expected,"receipt lost original source request")
    let raw=receipt.raw
    try validateMLXCommon(raw,kind:"EVAL_RECEIPT",session:expected.session)
    let seq=try receipt.evalSequence,lease=try receipt.wireLease
    try runtimeRequire(seq == lastSequence+1 && lease == lastLease+1,"wire command/lease order")
    try runtimeRequire(raw.field("request_id").text() == expected.commandID && raw.field("source_context") == expected.json &&
        raw.field("input_sha256").text() == expected.inputSHA256 && backendInt(raw.field("token_count")) == UInt64(expected.tokens.count),"receipt source/input mismatch")
    try runtimeRequire(receipt.receiptID == expected.session.session+"/"+expected.session.worker+"/eval/"+String(seq) && raw.field("sealed").boolean(),"unsealed or foreign receipt")
    try runtimeRequire(receipt.token < 151936,"receipt token outside vocabulary")
    let begin=try backendInt(raw.field("eval_begin_ns")),end=try backendInt(raw.field("eval_end_ns")),item=try backendInt(raw.field("item_end_ns"))
    try runtimeRequire(begin <= end && end <= item && item <= backendInt(raw.field("worker_ns")),"host eval/item observation order")
    let layers=try receipt.layers;try runtimeRequire(layers.count == 28,"incomplete eval layer set")
    var proposed=pageHistory
    for (index,row) in layers.enumerated() {
        let pool=expected.session.pools[index];try validateMLXPool(row,pool:pool)
        try runtimeRequire(backendInts(row.field("cache_page_ids")) == [0] && backendInts(row.field("used_page_ids")) == [0] && backendInts(row.field("free_page_ids")).isEmpty &&
            backendInts(row.field("page_lens")) == [UInt64(expected.tokens.count)] && backendInt(row.field("offset")) == UInt64(expected.tokens.count),"prefill logical page extent")
        let pages=try row.field("owned_pages").elements();try runtimeRequire(pages.count == 1,"missing original owned page")
        _ = try pages[0].members(exactly:["page_id","allocation_epoch"])
        let page=try backendInt(pages[0].field("page_id")),allocation=try backendInt(pages[0].field("allocation_epoch"))
        let key=pool.poolID+"/page/"+String(page)
        try runtimeRequire(page == 0 && allocation == (pageHistory[key] ?? 0)+1,"actual page allocation history mismatch")
        proposed[key]=allocation
    }
    return proposed
}

func validateMLXReturn(_ ack:MLXReturnACK,expected:MLXReturnRequest)throws {
    try runtimeRequire(ack.request == expected,"ACK lost original return request")
    let receipt=expected.receipt,raw=ack.raw
    try validateMLXCommon(raw,kind:"RELEASE_ACK",session:receipt.request.session)
    try runtimeRequire(raw.field("source_context") == receipt.request.json && raw.field("request_id").text() == receipt.request.commandID &&
        backendInt(raw.field("lease_epoch")) == receipt.wireLease && backendInt(raw.field("eval_seq")) == receipt.evalSequence &&
        backendInt(raw.field("seq")) == receipt.evalSequence+1 && raw.field("receipt_id").text() == receipt.receiptID && raw.field("released").boolean(),"release acknowledgement identity")
    let rows=try raw.field("layers").elements(),prior=try receipt.layers
    try runtimeRequire(rows.count == 28,"partial layer release")
    for (i,row) in rows.enumerated() {
        _ = try row.members(exactly:["layer","pool_id","before","released_pages","after"])
        try runtimeRequire(backendInt(row.field("layer")) == UInt64(i) && row.field("pool_id").text() == receipt.request.session.pools[i].poolID &&
            row.field("before") == prior[i] && row.field("released_pages") == prior[i].field("owned_pages"),"release does not match original lease pages")
        let after=try row.field("after");try validateMLXPool(after,pool:receipt.request.session.pools[i])
        for key in ["cache_page_ids","page_lens","owned_pages","used_page_ids"] {try runtimeRequire(after.field(key) == .array([]),"release retains live cache/page metadata")}
        try runtimeRequire(backendInt(after.field("offset")) == 0 && backendInts(after.field("free_page_ids")) == [0],"release pool not empty")
        for key in ["layer","pool_id","pool_object_id","num_pages","capacity","storage_nbytes"] {try runtimeRequire(after.field(key) == prior[i].field(key),"release changed backing identity/geometry")}
    }
}

func validateMLXClosed(_ closed:MLXClosedEvidence,session:MLXSessionIdentity,lastSequence:UInt64)throws {
    try runtimeRequire(closed.session == session && closed.complete,"backend process/IO not actually closed")
    try validateMLXCommon(closed.shutdownACK,kind:"SHUTDOWN_ACK",session:session)
    try runtimeRequire(backendInt(closed.shutdownACK.field("seq")) == lastSequence+1 && closed.shutdownACK.field("all_released").boolean(),"shutdown without all release acknowledgements")
    let pools=try closed.shutdownACK.field("pools").elements();try runtimeRequire(pools.count == 28,"shutdown pool set")
    for (i,pool) in pools.enumerated() {
        try validateMLXPool(pool,pool:session.pools[i])
        for key in ["cache_page_ids","page_lens","owned_pages","used_page_ids"] {try runtimeRequire(pool.field(key) == .array([]),"shutdown live pool")}
        try runtimeRequire(backendInt(pool.field("offset")) == 0,"shutdown live offset")
    }
}
