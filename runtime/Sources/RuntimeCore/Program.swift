import Foundation
import CryptoKit

package struct FrontendRejection: Error, Sendable {
    package let status: String
    package let diagnostic: JSONValue
    package let exitCode: Int32
    package init(status:String,diagnostic:JSONValue,exitCode:Int32) { self.status=status;self.diagnostic=diagnostic;self.exitCode=exitCode }
}
package struct ProtocolError: Error, Sendable {
    package let diagnostic: JSONValue
    package init(diagnostic:JSONValue) { self.diagnostic=diagnostic }
    package init(_ message: String) {
        diagnostic = .object(["phase":.string("protocol"),"code":.string("invalid-compiler-exchange"),
                              "path":.array([]),"message":.string(message)])
    }
}
func demand(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !condition() { throw ProtocolError(message) }
}
func decimalIndex(_ text: String) throws -> Int {
    try demand(text == "0" || (text.first != "0" && !text.isEmpty && text.utf8.allSatisfy { (48...57).contains($0) }), "canonical index")
    guard let result = Int(text), result >= 0 else { throw ProtocolError("index overflow") }
    return result
}
func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format:"%02x",$0) }.joined() }
func pointerChild(_ parent: String, _ component: String) -> String {
    parent + "/" + component.replacingOccurrences(of:"~",with:"~0").replacingOccurrences(of:"/",with:"~1")
}
func unsignedToken(_ text: String) throws -> UInt64 {
    let canonical = text == "-0" ? "0" : text
    try demand(canonical == "0" || (!canonical.isEmpty && canonical.first != "0" && canonical.utf8.allSatisfy { (48...57).contains($0) }), "unsigned integer required")
    guard let n = UInt64(canonical) else { throw ProtocolError("Nat outside executable UInt64 profile") }
    return n
}
func wordToken(_ text: String) throws -> UInt64 {
    if text.first == "-" {
        let magnitude = try unsignedToken(String(text.dropFirst()))
        try demand(magnitude <= UInt64(1)<<63,"negative Word range")
        return 0 &- magnitude
    }
    return try unsignedToken(text)
}

struct SlotID: Hashable, Sendable {
    let site: String; let role: String; let index: Int
    var key: String { "\(site)#\(role):\(index)" }
}
struct NominalID: Hashable, Sendable {
    let site: String; let role: String; let index: Int; let sort: String
    var key: String { "\(site)#\(role):\(index):\(sort)" }
}
struct Binding: Sendable { let id: SlotID; let name: String; let type: JSONValue }
struct Capture: Sendable { let binder: SlotID; let source: SlotID; let move: Bool; let type: JSONValue }
struct FramePlan: Sendable { let kind: String; let retained: [SlotID]; let exception: String; let backendReturnContract:JSONValue? }
struct Generation: Sendable {
    let kind: String; let names: [NominalID]; let interface: JSONValue
    let success: [(SlotID,String,[NominalID])]
}
struct Annotation: Sendable {
    let path: String; let kind: String; let execution: String; let type: JSONValue; let effect: JSONValue
    let bindings: [Binding]; let copy: [SlotID]; let move: [SlotID]; let remaining: [SlotID]?
    let captures: [Capture]; let frame: FramePlan?; let generation: Generation?
    func binder(_ role: String, _ index: Int = 0) throws -> Binding {
        let matches = bindings.filter { $0.id.role == role && $0.id.index == index }
        try demand(matches.count == 1,"missing unique binder \(path) \(role)/\(index)")
        return matches[0]
    }
}

/// Retains complete static interfaces. Ref expansion is lazy and never expands a proof.
struct StaticTable: Sendable {
    let values: [JSONValue]
    init(_ raw: JSONValue) throws {
        values = try raw.elements()
        try demand(values.count <= 131072,"static table limit")
        for (index, value) in values.enumerated() { try Self.validate(value, before:index, depth:0) }
    }
    private static func validate(_ term: JSONValue, before: Int, depth: Int) throws {
        try demand(depth <= 128,"static term nesting limit")
        switch term {
        case .null,.bool,.string: return
        case .object(let object):
            try demand(object.count == 1,"static term exact union")
            if let r = object["ref"] { try demand(try decimalIndex(r.text()) < before,"static ref postorder"); return }
            if let integer = object["integer"] {
                let s = try integer.text(); let digits = s.first == "-" ? String(s.dropFirst()) : s
                try demand(s != "-0" && !digits.isEmpty && (digits == "0" || digits.first != "0") && digits.utf8.allSatisfy { (48...57).contains($0) },"static canonical integer")
                return
            }
            if let array = object["array"] { for child in try array.elements() { try validate(child,before:before,depth:depth+1) }; return }
            if let members = object["object"] {
                var seen = Set<String>()
                for pair in try members.elements() {
                    let xs = try pair.elements(); try demand(xs.count == 2,"static member pair")
                    let key = try xs[0].text()
                    try demand(key.utf8.allSatisfy { $0 < 128 } && seen.insert(key).inserted,"static object key")
                    try validate(xs[1],before:before,depth:depth+1)
                }; return
            }
            throw ProtocolError("unknown static term")
        default: throw ProtocolError("bare number/array in static term")
        }
    }
    func resolve(_ input: JSONValue) throws -> JSONValue {
        var value = input; var budget = values.count+1
        while case .object(let o) = value, let ref = o["ref"] {
            try demand(o.count == 1 && budget > 0,"static ref shape/cycle")
            let index = try decimalIndex(ref.text()); try demand(index < values.count,"static reference bounds")
            value = values[index]; budget -= 1
        }
        return value
    }
    func reference(_ input: JSONValue) throws {
        _ = try input.members(exactly:["ref"])
        try demand(try decimalIndex(input.field("ref").text()) < values.count,"static root reference")
    }
    func object(_ input: JSONValue) throws -> [String:JSONValue] {
        let pairs = try resolve(input).field("object").elements(); var result:[String:JSONValue] = [:]
        for pair in pairs { let xs = try pair.elements(); result[try xs[0].text()] = xs[1] }
        return result
    }
    func field(_ input: JSONValue, _ key: String) throws -> JSONValue {
        guard let found = try object(input)[key] else { throw ProtocolError("missing static field \(key)") }; return found
    }
    func text(_ input: JSONValue) throws -> String { try resolve(input).text() }
    func fields(_ input: JSONValue) throws -> [String:JSONValue] { try object(field(input,"fields")) }
    func tag(_ input: JSONValue) throws -> String { try text(field(input,"tag")) }
    private struct ComparedReferences: Hashable {
        let left: Int; let right: Int
    }
    /// Full structural correspondence in the already validated postorder DAG.
    /// Distinct refs can encode the same object with different member order.
    /// No alpha-renaming, subtyping or omission of interface fields is allowed.
    func equivalent(_ left: JSONValue, _ right: JSONValue) throws -> Bool {
        var pending: [(JSONValue,JSONValue)] = []
        var seen = Set<ComparedReferences>()
        var remaining = 1_048_576
        func push(_ a: JSONValue, _ b: JSONValue) throws {
            try demand(remaining > 0,"static equivalence work limit")
            remaining -= 1; pending.append((a,b))
        }
        func index(_ term: JSONValue) throws -> Int? {
            guard case .object(let fields) = term, let ref = fields["ref"] else { return nil }
            try demand(fields.count == 1,"static equivalence ref shape")
            let i = try decimalIndex(ref.text())
            try demand(i < values.count,"static equivalence ref bounds")
            return i
        }
        try push(left,right)
        while let (a,b) = pending.popLast() {
            let ai = try index(a), bi = try index(b)
            if let ai, let bi {
                if ai == bi { continue }
                if !seen.insert(ComparedReferences(left:ai,right:bi)).inserted { continue }
                try push(values[ai],values[bi]); continue
            }
            if let ai { try push(values[ai],b); continue }
            if let bi { try push(a,values[bi]); continue }
            switch (a,b) {
            case (.null,.null): break
            case (.bool(let av),.bool(let bv)): if av != bv { return false }
            case (.string(let av),.string(let bv)): if av != bv { return false }
            case (.object(let av),.object(let bv)):
                if let integer = av["integer"] {
                    if bv["integer"] != integer { return false }
                } else if let array = av["array"] {
                    guard let other = bv["array"] else { return false }
                    let xs = try array.elements(), ys = try other.elements()
                    if xs.count != ys.count { return false }
                    try demand(xs.count <= remaining,"static equivalence work limit")
                    for i in xs.indices { try push(xs[i],ys[i]) }
                } else {
                    guard av["object"] != nil, bv["object"] != nil else { return false }
                    let xs = try object(a), ys = try object(b)
                    if xs.count != ys.count { return false }
                    try demand(xs.count <= remaining,"static equivalence work limit")
                    for key in xs.keys.sorted() {
                        guard let other = ys[key] else { return false }
                        try push(xs[key]!,other)
                    }
                }
            default: return false
            }
        }
        return true
    }
    func copyable(_ input: JSONValue, depth: Int = 0) throws -> Bool {
        try demand(depth < 128,"type depth")
        let obj = try object(input)
        if obj["binder"] != nil { return try copyable(field(input,"body"),depth:depth+1) }
        let tag = try text(field(input,"tag")); let fields = try self.fields(input)
        if tag == "Write" { return false }
        if tag == "Fn" { guard let mode=fields["mode"] else {throw ProtocolError("Fn mode")};return try text(mode) == "U" }
        if tag == "Pair" || tag == "Sum" {
            guard let left=fields["left"],let right=fields["right"] else {throw ProtocolError("product/sum fields")}
            return try copyable(left,depth:depth+1) && copyable(right,depth:depth+1)
        }
        return true
    }
    /// Only small protocol identities use this helper; full types remain indexed.
    func smallValue(_ input:JSONValue,remaining:inout Int)throws->JSONValue {
        remaining -= 1;try demand(remaining >= 0,"small static identity limit")
        let value=try resolve(input)
        switch value {
        case .null,.bool,.string:return value
        case .object(let o):
            if let n=o["integer"] { return .number(try n.text()) }
            if let xs=o["array"] { return .array(try xs.elements().map {try smallValue($0,remaining:&remaining)}) }
            var fields:[String:JSONValue]=[:]
            for (key,term) in try object(value) {fields[key]=try smallValue(term,remaining:&remaining)}
            return .object(fields)
        default:throw ProtocolError("malformed small static value")
        }
    }
}

package struct CheckedProgram: Sendable {
    let source: JSONValue; let sourceBytes: Data; let sourceHash: String; let rawHash: String
    package let executionDomain: CheckedExecutionDomain
    let responseHash: String
    package var sourceBytesSHA256: String { rawHash }
    package var canonicalSourceSHA256: String { sourceHash }
    package var compilerResponseSHA256: String { responseHash }
    let table: StaticTable; let annotations: [String:Annotation]; let nodes: [String:JSONValue]
    let nominalEnvironments: [String:[String:NominalID]]
    let entryType: JSONValue; let entryEffect: JSONValue
    func annotation(_ path: String) throws -> Annotation {
        guard let a = annotations[path] else { throw ProtocolError("missing site \(path)") }; return a
    }
}

private struct ProgramDecoder {
    let hash: String; let table: StaticTable
    func site(_ raw: JSONValue) throws -> String {
        _ = try raw.members(exactly:["source_sha256","pointer"])
        try demand(try raw.field("source_sha256").text() == hash,"site source identity")
        let p = try raw.field("pointer").text(); try demand(p == "/main/body" || p.hasPrefix("/main/body/"),"site outside main body")
        return p
    }
    func slot(_ raw: JSONValue) throws -> SlotID {
        _ = try raw.members(exactly:["site","role","index"])
        let role = try raw.field("role").text()
        try demand(["parameter","capture","let","case","handler","generated","cleanup-parameter"].contains(role),"term role")
        return try SlotID(site:site(raw.field("site")),role:role,index:decimalIndex(raw.field("index").text()))
    }
    func nominal(_ raw: JSONValue) throws -> NominalID {
        _ = try raw.members(exactly:["site","role","index","sort"])
        let sort = try raw.field("sort").text(); let role = try raw.field("role").text()
        try demand(["Size","Owner","Epoch","Scope","Region","Invocation","Job","Task","Access"].contains(sort),"nominal sort")
        try demand(["init-private","new-owner","scope","run","produce","fork","unpack"].contains(role),"nominal role")
        return try NominalID(site:site(raw.field("site")),role:role,index:decimalIndex(raw.field("index").text()),sort:sort)
    }
    func slots(_ raw: JSONValue) throws -> [SlotID] { try raw.elements().map(slot) }
    func annotation(_ raw: JSONValue) throws -> Annotation {
        _ = try raw.members(exactly:["site","node_kind","execution","type","effect","bindings","usage","capture","frame","generation"])
        let path = try site(raw.field("site")); let type = try raw.field("type"); let effect = try raw.field("effect")
        if type != .null { try table.reference(type) }; try table.reference(effect)
        let execution = try raw.field("execution").text()
        try demand(["reachable","ghost-only","unreachable-continuation"].contains(execution),"execution classification")
        let bindings = try raw.field("bindings").elements().map { value -> Binding in
            _ = try value.members(exactly:["binder","name","type"]); let id = try slot(value.field("binder"))
            try demand(id.site == path,"binding owner site"); let typ = try value.field("type"); try table.reference(typ)
            return try Binding(id:id,name:value.field("name").text(),type:typ)
        }
        try demand(Set(bindings.map(\.id)).count == bindings.count,"duplicate binding")
        let usage = try raw.field("usage"); _ = try usage.members(exactly:["copy","move","remaining"])
        let remaining = try usage.field("remaining"); let moves = try slots(usage.field("move"))
        try demand(Set(moves).count == moves.count,"duplicate direct move")
        let captures = try raw.field("capture").elements().map { value -> Capture in
            _ = try value.members(exactly:["binder","source","use","type"])
            let use = try value.field("use").text(); try demand(["copy","move"].contains(use),"capture usage")
            let typ = try value.field("type"); try table.reference(typ)
            return try Capture(binder:slot(value.field("binder")),source:slot(value.field("source")),move:use == "move",type:typ)
        }
        var frame:FramePlan? = nil; let rawFrame = try raw.field("frame")
        if rawFrame != .null {
            let frameKind = try rawFrame.field("kind").text()
            let frameFields:Set<String> = ["kind","retained","normal_restore","exception_policy"]
            _ = try rawFrame.members(exactly:frameKind == "ScopeExit" ? frameFields.union(["backend_return_contract"]) : frameFields)
            let kind = try rawFrame.field("kind").text(); let policy = try rawFrame.field("exception_policy").text()
            let policies = ["Bind":"unwind-drop","GenBind":"unwind-drop","CallReturn":"unwind-drop","Try":"try-restore","ScopeExit":"scope-drain-before-unwind"]
            try demand(policies[kind] == policy && rawFrame.field("normal_restore").text() == "move-once","frame policy")
            let kept = try slots(rawFrame.field("retained")); try demand(Set(kept).count == kept.count,"duplicate retained slot")
            let contract:JSONValue?
            if kind == "ScopeExit" {contract=try rawFrame.field("backend_return_contract")} else {contract=nil}
            frame = FramePlan(kind:kind,retained:kept,exception:policy,backendReturnContract:contract)
        }
        var generation:Generation? = nil; let g = try raw.field("generation")
        if g != .null {
            _ = try g.members(exactly:["kind","nominals","interface","success_bindings"])
            let kind = try g.field("kind").text(); try demand(["new-owner","scope","run","produce","fork","unpack"].contains(kind),"generation kind")
            let names = try g.field("nominals").elements().map(nominal)
            try demand(Set(names).count == names.count && names.allSatisfy { $0.site == path && $0.role == kind },"generation identities")
            let interface = try g.field("interface"); try table.reference(interface)
            let success = try g.field("success_bindings").elements().map { row -> (SlotID,String,[NominalID]) in
                _ = try row.members(exactly:["binder","kind","nominals"]); let kind = try row.field("kind").text()
                try demand(["Owner","Scope","Shared","Control","Read"].contains(kind),"success binding kind")
                return try (slot(row.field("binder")),kind,row.field("nominals").elements().map(nominal))
            }
            generation = Generation(kind:kind,names:names,interface:interface,success:success)
        }
        return try Annotation(path:path,kind:raw.field("node_kind").text(),execution:execution,type:type,effect:effect,bindings:bindings,
            copy:slots(usage.field("copy")),move:moves,remaining:remaining == .null ? nil : slots(remaining),captures:captures,frame:frame,generation:generation)
    }
}

package func decodeCompilerExchange(source: Data, response: Data, actualExitCode: Int32) throws -> CheckedProgram {
    do { return try decodeExchange(source:source,response:response,actualExitCode:actualExitCode) }
    catch let e as FrontendRejection { throw e }
    catch let e as ProtocolError { throw e }
    catch { throw ProtocolError(String(describing:error)) }
}
private func decodeExchange(source: Data, response: Data, actualExitCode: Int32) throws -> CheckedProgram {
    let result = try ExactJSON.decode(response,limits:.init(bytes:64*1024*1024,depth:256,values:2_000_000,stringBytes:2*1024*1024))
    _ = try result.members(exactly:["schema","status","program","diagnostic"])
    try demand(try result.field("schema").text() == "p8-runtime-frontend-response-v1","response version")
    let status = try result.field("status").text()
    let codes:[String:Int32] = ["ready":0,"static-rejected":1,"execution-profile-not-supported":3,"infrastructure-error":2]
    try demand(codes[status] == actualExitCode,"response status and actual exit")
    if status != "ready" {
        try demand(try result.field("program") == .null,"rejection contains executable program")
        let d = try result.field("diagnostic"); let fields = try d.members()
        try demand(Set(["phase","code","path","message"]).isSubset(of:Set(fields.keys)),"frontend diagnostic fields")
        throw FrontendRejection(status:status,diagnostic:d,exitCode:actualExitCode)
    }
    try demand(try result.field("diagnostic") == .null,"ready diagnostic")
    let program = try result.field("program")
    _ = try program.members(exactly:["schema","profile","source","sigma","static_values","sites","entry"])
    try demand(try program.field("schema").text() == "p8-runtime-checked-program-v1","program version")
    let profile=try program.field("profile").text()
    let domain:CheckedExecutionDomain
    switch profile {
    case "src1-response-offer-owner1-v1":domain = .mlxLeaf
    case "src1b-parent-host-close-v1":domain = .hostParent
    default:throw ProtocolError("program version")
    }
    let sourceRecord = try program.field("source"); _ = try sourceRecord.members(exactly:["utf8_base64","bytes_sha256","canonical_sha256"])
    let rawHash = digest(source)
    try demand(try Data(base64Encoded:sourceRecord.field("utf8_base64").text()) == source && sourceRecord.field("bytes_sha256").text() == rawHash,"exact source bytes")
    let ast = try ExactJSON.decode(source,limits:.init(bytes:1024*1024,depth:128,values:200_000,stringBytes:1024*1024))
    let hash = try digest(ast.sourceCanonicalEncoded())
    try demand(try sourceRecord.field("canonical_sha256").text() == hash,"canonical source identity")
    _ = try ast.members(exactly:["schema","main"])
    try demand(try ast.field("schema").text() == "p8-runtime-closed-source-v0.1","closed source only")
    _ = try ast.field("main").members(exactly:["result","effect","body"])
    let sigma = try program.field("sigma")
    _ = try sigma.members(exactly:domain == .mlxLeaf ? ["id","abi","operations","backend_contract","host_input_contract","host_response_contract"] : ["id","abi","operations","host_parent_contract","host_service_contract","tool_contract"])
    let expectedSigma:JSONValue = .object(["id":.string("p8-src1-fixed-sigma-v1"),"abi":.string("p8-src1-runtime-sigma-abi-v1"),"backend_contract":cm1BackendContract,"host_input_contract":.object([
        "id":.string("p8-ebd1-external-request-v1"),"port":.string("B"),"command":.string("receive_request"),
        "result_type":.string("Tokens8"),"caller":.string("init-main-job-only"),
        "owner_authority":.string("identity-only-no-storage-access"),"submission":.string("single-irrevocable-complete-request-v1")]),"host_response_contract":.object([
        "id":.string("src1-explicit-response-intent-v1"),"port":.string("B"),"registry_name":.string("B.response"),
        "command":.string("offer_response"),"payload_type":.string("Word"),"result_type":.string("Unit"),
        "caller":.string("init-main-job-only"),"owner_authority":.string("identity-only-no-storage-access"),
        "submission":.string("single-valid-intent-after-receipt-v1"),"commit":.string("original-main-joined-all-source-safe-v1")]),"operations":.array([
        .object(["name":.string("Prefill32"),"kind":.string("kernel"),"implementation":.string("qwen3-week3-mlx-prefill32-token1-v1")]),
        .object(["name":.string("Prefill8"),"kind":.string("kernel"),"implementation":.string("qwen3-week3-mlx-prefill8-token1-v1")]),
        .object(["name":.string("body_error"),"kind":.string("pure"),"implementation":.string("body-error-unit-v1")]),
        .object(["name":.string("B"),"kind":.string("host-input"),"implementation":.string("ebd1-receive-request-tokens8-v1")]),
        .object(["name":.string("B.response"),"kind":.string("host-response"),"implementation":.string("src1-offer-response-word-v1")])])])
    try demand(sigma == (domain == .mlxLeaf ? expectedSigma : parentSigma),"fixed Sigma implementations")
    try validateProfileDeclarations(ast,domain:domain)
    let table = try StaticTable(program.field("static_values")); let decoder = ProgramDecoder(hash:hash,table:table)
    let rawSites = try program.field("sites").elements(); try demand(rawSites.count <= 8192,"site limit")
    let siteRows = try rawSites.map(decoder.annotation)
    try demand(Set(siteRows.map(\.path)).count == siteRows.count,"duplicate source site")
    let annotations = Dictionary(uniqueKeysWithValues:siteRows.map { ($0.path,$0) })
    var nodes:[String:JSONValue] = [:]; var order:[String] = []; var names:[String:[String:NominalID]] = [:]
    var parentScopes:[String:NominalID] = [:]
    try walkSource(try ast.field("main").field("body"),family:"command",path:"/main/body",nominals:[:],annotations:annotations,domain:domain,privateMain:true,currentScope:nil,parentScopes:&parentScopes,nodes:&nodes,order:&order,names:&names)
    try demand(order == siteRows.map(\.path),"exact source occurrence order")
    try checkBindings(siteRows,nodes:nodes,decoder:decoder)
    try checkParentBindings(siteRows,nodes:nodes,scopes:parentScopes,decoder:decoder)
    let entry = try program.field("entry"); _ = try entry.members(exactly:["site","result_type","effect"])
    try demand(try decoder.site(entry.field("site")) == "/main/body","entry source site")
    let type = try entry.field("result_type"); let effect = try entry.field("effect"); try table.reference(type); try table.reference(effect)
    try demand(annotations["/main/body"]?.type == type && annotations["/main/body"]?.effect == effect,"entry annotation correspondence")
    return CheckedProgram(source:ast,sourceBytes:source,sourceHash:hash,rawHash:rawHash,executionDomain:domain,responseHash:digest(response),table:table,annotations:annotations,nodes:nodes,nominalEnvironments:names,entryType:type,entryEffect:effect)
}

/// Trusted entry configuration is checked before creating worker or request Tasks.
/// Native protocol fixtures may exercise rejects through the internal execute API.
package func validateExternalDemandConfiguration(_ program:CheckedProgram,configured:Bool)throws {
    try demand(program.executionDomain == .mlxLeaf,"external demand requires mlxLeaf domain")
    let receives=program.annotations.values.filter{$0.kind == "command:receive_request" && $0.execution == "reachable"}
    try demand(receives.count == (configured ? 1:0),"external request configuration/checked source receive count")
    // An occurrence check only: neither a count nor a may-effect proves that
    // a normal execution actually offers. Missing intent is a dynamic contract.
    let offers=program.annotations.values.filter{$0.kind == "command:offer_response"}
    try demand(configured || offers.isEmpty,"source response offer requires external request configuration")
}

private func walkSource(_ raw: JSONValue, family: String, path: String, nominals: [String:NominalID], annotations: [String:Annotation],
                        domain:CheckedExecutionDomain,privateMain:Bool,currentScope:NominalID?,parentScopes:inout [String:NominalID],
                        nodes: inout [String:JSONValue], order: inout [String], names: inout [String:[String:NominalID]]) throws {
    let tag = try raw.field(family).text()
    guard let a = annotations[path] else { throw ProtocolError("missing AST annotation") }
    try demand(a.kind == family+":"+tag,"AST/annotation kind")
    nodes[path]=raw; order.append(path); names[path]=nominals
    if family == "command" {
        if domain == .hostParent { try demand(!parentForbidden.contains(tag),"command prohibited by parent profile") }
        if tag == "call_service" || tag == "call_tool" {
            try demand(domain == .hostParent,"parent command prohibited by child profile")
            try demand(privateMain,"parent command requires original INIT main Job")
            guard let currentScope else {throw ProtocolError("parent command requires current plain scope")}
            parentScopes[path]=currentScope
        }
    }
    var fields:[String]=[]; var children:[(String,String)] = []; var bodyNominals = nominals
    switch (family,tag) {
    case ("value","unit"): break
    case ("value","var"): fields=["name"]
    case ("value","bool"): fields=["literal"]; _ = try raw.field("literal").boolean()
    case ("value","word"): fields=["literal"]; _ = try wordToken(raw.field("literal").integerToken())
    case ("value","nat"): fields=["literal"]; _ = try unsignedToken(raw.field("literal").integerToken())
    case ("value","pair"): fields=["left","right"]; children=[("left","value"),("right","value")]
    case ("value","inl"),("value","inr"): fields=["payload","sum_type"]; children=[("payload","value")]
    case ("value","pack"):
        fields=["exists_type","witnesses","payload"]; children=[("payload","value")]
        for witness in try raw.field("witnesses").elements() {
            let fields=try witness.members();try demand(fields.count == 1,"PACK witness union")
            if let name=fields["name"] {try demand(try nominals[name.text()] != nil,"PACK free witness")}
            else if let size=fields["size"] {
                if case .number(let token)=size {_ = try unsignedToken(token)}
                else {_ = try size.members(exactly:["witness"]);try demand(try nominals[size.field("witness").text()]?.sort == "Size","PACK symbolic size")}
            } else {throw ProtocolError("PACK witness shape")}
        }
    case ("value","lambda"): fields=["mode","captures","foot","projections","requires","links","effect","parameter","result","body"]; children=[("body","command")]
    case ("expression","value"),("expression","fst"),("expression","snd"): fields=["value"]; children=[("value","value")]
    case ("expression","op"):
        fields=["name","arguments"]; try demand(try raw.field("name").text() == "body_error","fixed source op")
        for i in try raw.field("arguments").elements().indices { children.append(("arguments/\(i)","value")) }
    case ("command","return"): fields=["value"]; children=[("value","value")]
    case ("command","let_e"): fields=["name","expression","next"]; children=[("expression","expression"),("next","command")]
    case ("command","let_c"):
        fields=["name","first","next"]; if try raw.members()["bind_type"] != nil { fields.append("bind_type") }; children=[("first","command"),("next","command")]
    case ("command","call"): fields=["function","argument"]; children=[("function","value"),("argument","value")]
    case ("command","if"): fields=["condition","then","else"]; children=[("condition","value"),("then","command"),("else","command")]
    case ("command","case"): fields=["value","left_name","left","right_name","right"]; children=[("value","value"),("left","command"),("right","command")]
    case ("command","unpack"): fields=["package","names","name","body"]; children=[("package","value"),("body","command")]
    case ("command","drop"): fields=["value","next"]; children=[("value","value"),("next","command")]
    case ("command","throw"): fields=["payload"]; children=[("payload","value")]
    case ("command","rethrow"): fields=["failure"]; children=[("failure","value")]
    case ("command","try"): fields=["body","failure_name","handler"]; children=[("body","command"),("handler","command")]
    case ("command","load"): fields=["read","index"]; children=[("read","value"),("index","value")]
    case ("command","receive_request"):
        fields=["name","owner"]; children=[("owner","value")]
        try demand(try raw.field("name").text() == "B","fixed host-input port")
    case ("command","offer_response"):
        fields=["name","owner","token"]; children=[("owner","value"),("token","value")]
        try demand(try raw.field("name").text() == "B","fixed host-response port")
    case ("command","call_service"):
        fields=["name","scope","tokens"]; children=[("scope","value"),("tokens","value")]
        try demand(try raw.field("name").text() == "B","fixed host-service name")
    case ("command","call_tool"):
        fields=["name","argument"]; children=[("argument","value")]
        try demand(try raw.field("name").text() == "tokenizer.vocab_symbol","fixed readonly-tool name")
    case ("command","await"),("command","await_cancel"): fields=["shared"]; children=[("shared","value")]
    case ("command","new_owner"):
        fields=["value","names","name","body"]; children=[("value","value"),("body","command")]
        let operand=try raw.field("value")
        try demand(try operand.field("value").text() == "nat","NEW profile requires literal Nat")
        let n=try unsignedToken(operand.field("literal").integerToken());try demand(n == 0 || n == 1,"NEW profile size")
    case ("command","scope"): fields=["names","name","body"]; children=[("body","command")]
    case ("command","run"): fields=["owner","names","name","body"]; children=[("owner","value"),("body","command")]
    case ("command","produce"):
        fields=["scope","kernel","argument","names","cleanup","result_name","control_name","next"]
        try demand(["Prefill32","Prefill8"].contains(try raw.field("kernel").text()),"fixed kernel")
        _ = try raw.field("cleanup").members(exactly:["captures","parameter","result","body"])
        children=[("scope","value"),("argument","value"),("cleanup/body","command"),("next","command")]
    case ("command","fork"):
        fields=["scope","names","job","result_name","control_name","next"]
        let job=try raw.field("job"); _ = try job.members(exactly:["captures","result","body"])
        let captures=try job.field("captures").elements().map {try $0.text()}
        try demand(Set(captures).count == captures.count,"duplicate fork captures")
        try demand(try raw.field("result_name").text() != raw.field("control_name").text(),"fork result/control names")
        children=[("scope","value"),("job/body","command"),("next","command")]
    case ("command","request_cancel"): fields=["control","next"]; children=[("control","value"),("next","command")]
    default: throw ProtocolError("unsupported node in ready program: \(family):\(tag)")
    }
    _ = try raw.members(exactly:Set([family]+fields))
    if a.frame?.kind == "ScopeExit" {
        try demand(a.frame?.backendReturnContract == (tag == "run" ? cm1BackendContract : JSONValue.null),"ScopeExit backend-return contract")
    }
    let generationKinds=["new_owner":"new-owner","scope":"scope","run":"run","produce":"produce","fork":"fork","unpack":"unpack"]
    let expectedGeneration = family == "command" ? generationKinds[tag]:nil
    try demand(a.generation?.kind == expectedGeneration,"source/generation kind correspondence")
    var expectedFrame:String?=nil
    if family == "command" {
        switch tag {
        case "new_owner","produce","fork":expectedFrame="GenBind"
        case "scope","run":expectedFrame="ScopeExit"
        case "call":expectedFrame="CallReturn"
        case "try":expectedFrame="Try"
        case "let_c":if !["call","try"].contains(try raw.field("first").field("command").text()) {expectedFrame="Bind"}
        default:break
        }
    }
    try demand(a.frame?.kind == expectedFrame,"source/frame kind correspondence")
    if let g=a.generation {
        let declared = try raw.field("names").elements()
        try demand(declared.count == g.names.count,"generation nominal arity")
        let fixedSorts=["new-owner":["Size","Owner"],"scope":["Scope","Region","Invocation"],"run":["Epoch","Scope","Region","Invocation"],"produce":["Job","Task","Invocation","Region","Access","Access","Region"],"fork":["Job","Task","Invocation","Region"]]
        if let required=fixedSorts[g.kind] {try demand(g.names.map(\.sort) == required,"generation nominal sorts")}
        for (i,row) in declared.enumerated() {
            _ = try row.members(exactly:["name","sort"])
            try demand(try row.field("sort").text() == g.names[i].sort && g.names[i].index == i,"generation nominal ordering")
            if g.kind == "fork" {try demand(try bodyNominals[row.field("name").text()] == nil,"fork fresh nominal name")}
            bodyNominals[try row.field("name").text()] = g.names[i]
        }
    }
    for (key,childFamily) in children {
        let components=key.split(separator:"/").map(String.init)
        var child=raw; var childPath=path
        for component in components {
            childPath=pointerChild(childPath,component)
            if case .array(let xs)=child, let i=Int(component) { child=xs[i] } else { child=try child.field(component) }
        }
        let insideGeneration = key == "body" || key == "cleanup/body" || key == "job/body" || (["produce","fork"].contains(tag) && key == "next")
        let childPrivate=privateMain && !(family == "value" && tag == "lambda" && key == "body") && key != "job/body" && key != "cleanup/body"
        var childScope=currentScope
        if family == "command" && tag == "scope" && key == "body" {
            guard let scope=a.generation?.names.first(where:{$0.sort == "Scope"}) else {throw ProtocolError("parent plain scope nominal")}
            childScope=scope
        }
        try walkSource(child,family:childFamily,path:childPath,nominals:insideGeneration ? bodyNominals : nominals,annotations:annotations,
                       domain:domain,privateMain:childPrivate,currentScope:childScope,parentScopes:&parentScopes,nodes:&nodes,order:&order,names:&names)
    }
}

/// Protocol correspondence, not a second type checker. Actual permissions are absent here.
private func checkBindings(_ rows:[Annotation],nodes:[String:JSONValue],decoder:ProgramDecoder)throws {
    var definitions:[SlotID:Binding]=[:];var nominalDefinitions=Set<NominalID>()
    let initialSorts=["Job","Task","Invocation","Invocation","Region","Region","Scope","Region"]
    for (index,sort) in initialSorts.enumerated() { nominalDefinitions.insert(NominalID(site:"/main/body",role:"init-private",index:index,sort:sort)) }
    for row in rows {
        for b in row.bindings { try demand(definitions[b.id] == nil,"duplicate lexical binding");definitions[b.id]=b }
        if let g=row.generation { nominalDefinitions.formUnion(g.names) }
    }
    for row in rows {
        let raw=nodes[row.path]!
        for id in row.copy+row.move+(row.remaining ?? [])+(row.frame?.retained ?? []) {try demand(definitions[id] != nil,"unknown checked slot")}
        for id in row.copy {try demand(try decoder.table.copyable(definitions[id]!.type),"copy annotation names affine slot")}
        for id in row.frame?.retained ?? [] {try demand(try !decoder.table.copyable(definitions[id]!.type),"frame retained Copy slot")}
        for c in row.captures {
            try demand(c.binder.site == row.path && c.binder.role == "capture" && definitions[c.source] != nil && definitions[c.binder] != nil,"capture lexical correspondence")
            try demand(definitions[c.binder]!.type == c.type && definitions[c.source]!.type == c.type,"capture complete type correspondence")
        }
        if row.kind == "value:var" {
            try demand(row.copy.count+row.move.count == 1,"var exact direct usage")
            let id=(row.copy+row.move)[0]
            try demand(try definitions[id]!.name == raw.field("name").text(),"var lexical name correspondence")
        }
        if row.kind == "command:receive_request" && row.execution == "reachable" {
            let table=decoder.table
            func word(_ type:JSONValue)throws {
                try demand(try table.text(table.field(type,"family")) == "type" && table.tag(type) == "Word" &&
                           table.fields(type).isEmpty,"host input result Word contract")
            }
            var result=row.type
            for _ in 0..<7 {
                try demand(try table.tag(result) == "Pair","host input complete Tokens8 contract")
                let fields=try table.fields(result)
                try demand(Set(fields.keys) == Set(["left","right"]),"host input Pair fields")
                try word(fields["left"]!);result=fields["right"]!
            }
            try word(result)
            guard let operand=rows.first(where:{$0.path == row.path+"/owner"}) else {throw ProtocolError("host input owner occurrence")}
            try demand(try table.tag(operand.type) == "Owner","host input Owner contract")
            let ownerFields=try table.fields(operand.type)
            guard let owner=ownerFields["owner"] else {throw ProtocolError("host input Owner identity")}
            try demand(try table.tag(row.effect) == "set","host input effect set")
            let effectFields=try table.fields(row.effect)
            guard let atomList=effectFields["atoms"] else {throw ProtocolError("host input effect atoms")}
            let atoms=try table.resolve(atomList).field("array").elements()
            try demand(atoms.count == 2,"host input exact effect arity")
            let input=try atoms.filter{try table.tag($0) == "HostInput"},raises=try atoms.filter{try table.tag($0) == "Raise"}
            try demand(input.count == 1 && raises.count == 1,"host input and Raise effects")
            let inputFields=try table.fields(input[0])
            try demand(try Set(inputFields.keys) == Set(["owner"]) && table.fields(raises[0]).isEmpty,
                       "host input effect fields")
            try demand(try table.equivalent(inputFields["owner"]!,owner),"host input effect Owner correspondence")
        }
        if row.kind == "command:offer_response" && row.execution == "reachable" {
            let table=decoder.table
            try demand(try table.text(table.field(row.type,"family")) == "type" &&
                       table.tag(row.type) == "Unit" && table.fields(row.type).isEmpty,"host response result Unit contract")
            guard let ownerOccurrence=rows.first(where:{$0.path == row.path+"/owner"}),
                  let tokenOccurrence=rows.first(where:{$0.path == row.path+"/token"}) else {
                throw ProtocolError("host response complete operand occurrences")
            }
            try demand(try table.text(table.field(tokenOccurrence.type,"family")) == "type" &&
                       table.tag(tokenOccurrence.type) == "Word" && table.fields(tokenOccurrence.type).isEmpty,"host response token Word contract")
            try demand(try table.text(table.field(ownerOccurrence.type,"family")) == "type" &&
                       table.tag(ownerOccurrence.type) == "Owner","host response Owner contract")
            let ownerFields=try table.fields(ownerOccurrence.type)
            guard let owner=ownerFields["owner"],Set(ownerFields.keys) == Set(["owner","size"]) else {
                throw ProtocolError("host response Owner identity")
            }
            // Bind the complete operand interface, not just two field names.
            // These are small identities; the full type comparison stays in
            // the bounded DAG and does not expand a proof or recheck typing.
            func concreteNominal(_ value:JSONValue,sort:String)throws {
                guard case .object(let fields)=value,Set(fields.keys) == Set(["nominal"]),
                      let raw=fields["nominal"] else {throw ProtocolError("host response \(sort) nominal form")}
                let name=try decoder.nominal(raw)
                try demand(name.sort == sort && nominalDefinitions.contains(name),"host response \(sort) nominal sort/source")
            }
            var ownerBudget=64
            try concreteNominal(table.smallValue(owner,remaining:&ownerBudget),sort:"Owner")
            var sizeBudget=64
            let size=try table.smallValue(ownerFields["size"]!,remaining:&sizeBudget)
            if case .object(let fields)=size,Set(fields.keys) == Set(["size_literal"]),
               case .number(let literal)?=fields["size_literal"] {
                try demand(!literal.hasPrefix("-"),"host response Size literal nonnegative")
            } else {
                try concreteNominal(size,sort:"Size")
            }
            if ownerOccurrence.kind == "value:var" {
                let uses=ownerOccurrence.copy+ownerOccurrence.move
                guard uses.count == 1,let binding=definitions[uses[0]] else {
                    throw ProtocolError("host response Owner lexical binding")
                }
                try demand(try table.equivalent(ownerOccurrence.type,binding.type),"host response Owner complete binding correspondence")
            }
            try demand(try table.text(table.field(row.effect,"family")) == "effect" &&
                       table.tag(row.effect) == "set","host response effect set")
            let effectFields=try table.fields(row.effect)
            guard let atomList=effectFields["atoms"],Set(effectFields.keys) == Set(["atoms"]) else {
                throw ProtocolError("host response effect atoms")
            }
            let atoms=try table.resolve(atomList).field("array").elements()
            try demand(atoms.count == 2,"host response exact effect arity")
            let response=try atoms.filter{try table.tag($0) == "HostResponse"},raises=try atoms.filter{try table.tag($0) == "Raise"}
            try demand(response.count == 1 && raises.count == 1,"host response and Raise effects")
            let responseFields=try table.fields(response[0])
            try demand(try table.text(table.field(response[0],"family")) == "atom" &&
                       table.text(table.field(raises[0],"family")) == "atom" &&
                       Set(responseFields.keys) == Set(["owner"]) && table.fields(raises[0]).isEmpty,"host response effect fields")
            try demand(try table.equivalent(responseFields["owner"]!,owner),"host response effect Owner correspondence")
        }
        var expected:[SlotID:String]=[:]
        func expect(_ role:String,_ index:Int,_ name:String) { expected[SlotID(site:row.path,role:role,index:index)]=name }
        switch row.kind {
        case "command:let_e","command:let_c":expect("let",0,try raw.field("name").text())
        case "command:case":expect("case",0,try raw.field("left_name").text());expect("case",1,try raw.field("right_name").text())
        case "command:try":expect("handler",0,try raw.field("failure_name").text())
        case "command:new_owner","command:scope","command:run","command:unpack":expect("generated",0,try raw.field("name").text())
        case "value:lambda":
            expect("parameter",0,try raw.field("parameter").field("name").text())
            for (i,n) in try raw.field("captures").elements().enumerated() {expect("capture",i,try n.text())}
        case "command:produce":
            expect("generated",0,try raw.field("result_name").text());expect("generated",1,try raw.field("control_name").text())
            expect("cleanup-parameter",0,try raw.field("cleanup").field("parameter").field("name").text())
            for (i,n) in try raw.field("cleanup").field("captures").elements().enumerated() {expect("capture",i,try n.text())}
        case "command:fork":
            expect("generated",0,try raw.field("result_name").text());expect("generated",1,try raw.field("control_name").text())
            for (i,n) in try raw.field("job").field("captures").elements().enumerated() {expect("capture",i,try n.text())}
        default:break
        }
        try demand(Dictionary(uniqueKeysWithValues:row.bindings.map{($0.id,$0.name)}) == expected,"exact source binding definitions")
        if let g=row.generation {
            for (id,_,refs) in g.success {try demand(definitions[id] != nil && refs.allSatisfy(nominalDefinitions.contains),"generated binding references")}
            if g.kind == "fork" {
                // The fixed compiler proves typing/capture lifetimes. Here we
                // bind its complete interface and result to this source site.
                let table=decoder.table
                try demand(try table.text(table.field(g.interface,"binder")) == "generation-fork","fork interface kind")
                let sorts=try table.resolve(table.field(g.interface,"ordered_sorts")).field("array").elements().map {try table.text($0)}
                try demand(sorts == ["Job","Task","Invocation","Region"],"fork interface ordered sorts")
                try demand(try table.tag(table.field(g.interface,"body")) == "Unit","fork interface body")
                try demand(g.success.count == 2,"fork success binding domain")
                for (index,tag) in ["Shared","Control"].enumerated() {
                    let binding=try row.binder("generated",index), success=g.success[index]
                    try demand(success.0 == binding.id && success.1 == tag,"fork success binding domain")
                    try demand(try table.tag(binding.type) == tag,"fork generated complete type")
                    let fields=try table.fields(binding.type)
                    guard let job=fields["job"] else {throw ProtocolError("fork generated Job")}
                    var budget=128
                    let rawJob=try table.smallValue(table.field(job,"nominal"),remaining:&budget)
                    try demand(try decoder.nominal(rawJob) == g.names[0],"fork generated Job identity")
                    if index == 0 {
                        guard let payload=fields["payload"],let body=rows.first(where:{$0.path == row.path+"/job/body"}) else {throw ProtocolError("fork child result interface")}
                        try demand(try table.equivalent(payload,body.type),"fork complete child result correspondence")
                    }
                }
            }
        }
    }
    let byPath=Dictionary(uniqueKeysWithValues:rows.map{($0.path,$0)})
    func lexical(_ path:String,_ environment:[String:SlotID])throws {
        let row=byPath[path]!,raw=nodes[path]!
        var direct:[SlotID]=[]
        if row.kind == "value:var" {
            guard let id=environment[try raw.field("name").text()] else {throw ProtocolError("var outside lexical environment")};direct=[id]
        }
        if ["value:lambda","command:produce","command:fork"].contains(row.kind) {
            let container=try row.kind == "command:produce" ? raw.field("cleanup") : row.kind == "command:fork" ? raw.field("job") : raw
            let names=try container.field("captures").elements()
            for (index,name) in names.enumerated() {
                guard let id=environment[try name.text()] else {throw ProtocolError("capture outside lexical environment")}
                direct.append(id)
                try demand(index < row.captures.count && row.captures[index].source == id && row.captures[index].binder == SlotID(site:path,role:"capture",index:index),"capture source/order")
                if row.kind == "command:fork" {try demand(try !row.captures[index].move && decoder.table.copyable(row.captures[index].type),"fork captures must Copy")}
            }
            try demand(row.captures.count == names.count,"capture domain")
        } else {try demand(row.captures.isEmpty,"capture on non-closure source")}
        var copies:[SlotID]=[],moves:[SlotID]=[]
        for id in direct {if try decoder.table.copyable(definitions[id]!.type) {copies.append(id)} else {moves.append(id)}}
        try demand(row.copy == copies && row.move == moves,"source-directed direct usage")
        func extended(_ base:[String:SlotID],_ role:String,_ index:Int=0)throws->[String:SlotID] {
            let b=try row.binder(role,index);var result=base;result[b.name]=b.id;return result
        }
        func visit(_ suffix:String,_ env:[String:SlotID]?=nil)throws {try lexical(path+"/"+suffix,env ?? environment)}
        switch row.kind {
        case "value:lambda":
            var inner:[String:SlotID]=[:]
            for i in row.captures.indices {inner=try extended(inner,"capture",i)}
            try visit("body",extended(inner,"parameter"))
        case "command:produce":
            try visit("scope");try visit("argument")
            var inner:[String:SlotID]=[:]
            for i in row.captures.indices {inner=try extended(inner,"capture",i)}
            try visit("cleanup/body",extended(inner,"cleanup-parameter"))
            try visit("next",extended(extended(environment,"generated",0),"generated",1))
        case "command:fork":
            try visit("scope")
            var inner:[String:SlotID]=[:]
            for i in row.captures.indices {inner=try extended(inner,"capture",i)}
            try visit("job/body",inner)
            try visit("next",extended(extended(environment,"generated",0),"generated",1))
        case "command:let_e":try visit("expression");try visit("next",extended(environment,"let"))
        case "command:let_c":try visit("first");try visit("next",extended(environment,"let"))
        case "command:case":try visit("value");try visit("left",extended(environment,"case",0));try visit("right",extended(environment,"case",1))
        case "command:try":try visit("body");try visit("handler",extended(environment,"handler"))
        case "command:new_owner":try visit("value");try visit("body",extended(environment,"generated"))
        case "command:unpack":try visit("package");try visit("body",extended(environment,"generated"))
        case "command:run":try visit("owner");try visit("body",extended(environment,"generated"))
        case "command:scope":try visit("body",extended(environment,"generated"))
        default:
            // Immediate actual source children; never enter annotation/interface values.
            for child in rows where child.path.hasPrefix(path+"/") {
                let suffix=String(child.path.dropFirst(path.count+1))
                if !suffix.contains("/") || (row.kind == "expression:op" && suffix.hasPrefix("arguments/") && suffix.split(separator:"/").count == 2) {
                    try lexical(child.path,environment)
                }
            }
        }
    }
    try lexical("/main/body",[:])
    // Each nominal object is a small leaf in the static table. Do not expand its enclosing type/Φ.
    for value in decoder.table.values {
        guard case .object(let encoded)=value,encoded["object"] != nil else {continue}
        let object=try decoder.table.object(value)
        if let identity=object["nominal"] {
            try demand(object.count == 1,"nominal static union")
            var budget=128;let raw=try decoder.table.smallValue(identity,remaining:&budget)
            try demand(try nominalDefinitions.contains(decoder.nominal(raw)),"static nominal lacks source/INIT definition")
        }
    }
}


private let parentSigma:JSONValue = .object(["id":.string("p8-src1b-parent-fixed-sigma-v1"),"abi":.string("p8-src1b-parent-runtime-sigma-abi-v1"),"operations":.array([.object(["name":.string("body_error"),"kind":.string("pure"),"implementation":.string("body-error-unit-v1")]),.object(["name":.string("B"),"kind":.string("host-service"),"implementation":.string("src1b-leaf-response-host-close-v1")]),.object(["name":.string("tokenizer.vocab_symbol"),"kind":.string("readonly-tool"),"implementation":.string("src1b-vocab-symbol-utf8-sha256-v1")])]),"host_parent_contract":.object(["id":.string("src1b-host-parent-v1"),"execution_domain":.string("hostParent"),"direct_backend":.string("none"),"caller":.string("init-main-job-only"),"scope_kind":.string("plain"),"service_instances":.number("1"),"scope_exit":.string("actual-host-close-before-parent-publication-v1"),"registry":.string("fixed-leaf-no-parent-callback-v1")]),"host_service_contract":.object(["id":.string("src1b-response-and-host-close-v1"),"name":.string("B"),"command":.string("call_service"),"argument_type":.string("Tokens8"),"result_type":.string("Word"),"child_profile":.string("src1-response-offer-owner1-v1"),"delivery":.string("after-source-join"),"response":.string("original-commit-requester-delivered-once-v1"),"responsibility":.string("parent-scope-HostClose-v1"),"submission":.string("reserve-gated-commit-start-v1")]),"tool_contract":.object(["id":.string("src1b-tokenizer-vocab-symbol-v1"),"name":.string("tokenizer.vocab_symbol"),"command":.string("call_tool"),"capability":.string("tokenizer_vocab_v1"),"argument_type":.string("Word"),"result_type":.string("ToolInfo-v1"),"representation":.string("bpe-vocab-symbol-utf8-v1"),"authority_sha256":.string("aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"),"max_file_bytes":.number("12582912"),"work_budget_ns":.number("2000000000"),"added_token_policy":.string("error-unsupported"),"missing_token_policy":.string("error-missing"),"effect":.string("readonly-file-io-plus-raise-v1")])])

private let parentForbidden:Set<String> = ["new_owner","run","produce","fork","receive_request","offer_response","await","await_cancel","request_cancel","load"]

private func validateProfileDeclarations(_ ast:JSONValue,domain:CheckedExecutionDomain)throws {
    var pending=[ast]
    while let item=pending.popLast() {
        switch item {
        case .object(let fields):
            if domain == .mlxLeaf,let atom=fields["atom"],case .string(let name)=atom {
                try demand(!["HostService","LocalRead"].contains(name),"parent effect prohibited by child profile")
            }
            pending.append(contentsOf:fields.values)
        case .array(let values):pending.append(contentsOf:values)
        default:break
        }
    }
}

/// Occurrence/profile gate only. Ledger realizes caller, admission, once and
/// response-consumed obligations; the fixed frontend supplies static proof.
package func validateParentServiceConfiguration(_ program:CheckedProgram)throws {
    try demand(program.executionDomain == .hostParent,"parent configuration requires hostParent domain")
    let commands=program.annotations.values.filter{$0.kind.hasPrefix("command:")}
    try demand(commands.filter{$0.kind == "command:call_service"}.count == 1,"parent service configuration requires one call_service occurrence")
    try demand(commands.filter{$0.kind == "command:call_tool"}.count == 1,"parent service configuration requires one call_tool occurrence")
    try demand(!commands.contains{parentForbidden.contains(String($0.kind.dropFirst(8)))},"command prohibited by parent profile")
}

/// Narrow correspondence for the two new command interfaces. This checks
/// resolved DAGs and source binding identities, not arbitrary whole-program proofs.
private func checkParentBindings(_ rows:[Annotation],nodes:[String:JSONValue],scopes:[String:NominalID],decoder:ProgramDecoder)throws {
    let table=decoder.table
    let indexed=Dictionary(uniqueKeysWithValues:rows.map{($0.path,$0)})
    func base(_ type:JSONValue,_ tag:String)throws {
        try demand(try table.text(table.field(type,"family")) == "type" && table.tag(type) == tag && table.fields(type).isEmpty,"parent fieldless \(tag) contract")
    }
    func pair(_ type:JSONValue)throws->[String:JSONValue] {
        try demand(try table.text(table.field(type,"family")) == "type" && table.tag(type) == "Pair","parent complete Pair contract")
        let fields=try table.fields(type)
        try demand(Set(fields.keys) == Set(["left","right"]),"parent Pair fields")
        return fields
    }
    func words(_ type:JSONValue,_ count:Int)throws {
        var current=type
        for _ in 1..<count {let fields=try pair(current);try base(fields["left"]!,"Word");current=fields["right"]!}
        try base(current,"Word")
    }
    func nominal(_ term:JSONValue,_ expected:NominalID)throws {
        var budget=128
        let value=try table.smallValue(term,remaining:&budget)
        _ = try value.members(exactly:["nominal"])
        try demand(try decoder.nominal(value.field("nominal")) == expected,"parent current scope nominal correspondence")
    }
    func bindingRegion(_ owner:Annotation,_ binding:Binding)throws->String? {
        switch owner.kind {
        case "command:let_e","command:let_c":return owner.path+"/next"
        case "command:scope","command:unpack":return owner.path+"/body"
        case "command:case":return owner.path+(binding.id.index == 0 ? "/left" : "/right")
        case "command:try":return owner.path+"/handler"
        default:return nil
        }
    }
    func operandBindings(_ root:String)throws {
        for variable in rows where variable.kind == "value:var" && (variable.path == root || variable.path.hasPrefix(root+"/")) {
            let name=try nodes[variable.path]!.field("name").text()
            var candidates:[(Int,Binding)]=[]
            for owner in rows {
                for binding in owner.bindings where binding.name == name {
                    if let region=try bindingRegion(owner,binding),variable.path.hasPrefix(region+"/") || variable.path == region {
                        candidates.append((region.count,binding))
                    }
                }
            }
            candidates.sort{$0.0 > $1.0}
            guard let nearest=candidates.first else {throw ProtocolError("parent operand has no visible lexical binding")}
            try demand(candidates.count == 1 || candidates[1].0 < nearest.0,"parent ambiguous lexical binding")
            try demand(variable.copy == [nearest.1.id] && variable.move.isEmpty,"parent operand exact Copy binding")
            try demand(try table.equivalent(variable.type,nearest.1.type),"parent operand complete binding type")
        }
    }
    for row in rows where row.kind == "command:call_service" || row.kind == "command:call_tool" {
        guard let expectedScope=scopes[row.path] else {throw ProtocolError("parent command private scope context")}
        let service=row.kind == "command:call_service"
        let parameterPath=row.path+(service ? "/tokens" : "/argument")
        guard let argument=indexed[parameterPath] else {throw ProtocolError("parent complete argument occurrence")}
        if service {
            try base(row.type,"Word");try words(argument.type,8)
            guard let scope=indexed[row.path+"/scope"],let owner=indexed[expectedScope.site],
                  let region=owner.generation?.names.first(where:{$0.sort == "Region"}) else {throw ProtocolError("parent complete scope occurrence")}
            try demand(try table.text(table.field(scope.type,"family")) == "type" && table.tag(scope.type) == "Scope","parent Scope type")
            let fields=try table.fields(scope.type)
            try demand(Set(fields.keys) == Set(["scope","region","kind"]),"parent complete Scope fields")
            try demand(try table.text(fields["kind"]!) == "plain","parent plain Scope kind")
            try nominal(fields["scope"]!,expectedScope);try nominal(fields["region"]!,region)
            try operandBindings(row.path+"/scope")
        } else {
            try base(argument.type,"Word")
            let fields=try pair(row.type);try base(fields["left"]!,"Nat");try words(fields["right"]!,4)
        }
        try operandBindings(parameterPath)
        try demand(try table.text(table.field(row.effect,"family")) == "effect" && table.tag(row.effect) == "set","parent exact effect set")
        let fields=try table.fields(row.effect)
        try demand(Set(fields.keys) == Set(["atoms"]),"parent exact effect fields")
        let atoms=try table.resolve(fields["atoms"]!).field("array").elements()
        try demand(atoms.count == 2,"parent exact effect arity")
        let effectName=service ? "HostService" : "LocalRead"
        let operations=try atoms.filter{try table.tag($0) == effectName},raises=try atoms.filter{try table.tag($0) == "Raise"}
        try demand(operations.count == 1 && raises.count == 1,"parent operation and Raise effects")
        for atom in atoms {try demand(try table.text(table.field(atom,"family")) == "atom","parent effect atom family")}
        try demand(try table.fields(raises[0]).isEmpty,"parent fieldless Raise")
        let operation=try table.fields(operations[0])
        if service {
            try demand(Set(operation.keys) == Set(["scope","service"]),"parent HostService exact fields")
            try demand(try table.text(operation["service"]!) == "B","parent HostService fixed literal")
            try nominal(operation["scope"]!,expectedScope)
        } else {
            try demand(Set(operation.keys) == Set(["capability"]),"parent LocalRead exact fields")
            try demand(try table.text(operation["capability"]!) == "tokenizer_vocab_v1","parent LocalRead fixed literal")
        }
        try demand(row.copy.isEmpty && row.move.isEmpty && row.bindings.isEmpty && row.captures.isEmpty && row.frame == nil && row.generation == nil,
                   "parent command exact direct usage/frame")
    }
}
