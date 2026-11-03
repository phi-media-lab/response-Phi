import Foundation
import Dispatch
import Synchronization
import CryptoKit

package enum VocabNativeFixture:String,Sendable {case openFailure,readFailure,schemaFailure,duplicateMapping,authorityMismatch,closeUnknown,byteLimit}
package struct VocabFileCapability:Sendable {
    let path:URL,expectedBytes:Int?,expectedSHA256:String?,purpose:VocabNativeFixture?
    package init(productionPath:URL)throws {
        try runtimeRequire(productionPath.isFileURL && productionPath.path.hasPrefix("/"),"absolute vocab file capability")
        path=productionPath;expectedBytes=11_422_654
        expectedSHA256="aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4";purpose=nil
    }
    private init(path:URL,bytes:Int?,sha:String?,purpose:VocabNativeFixture){self.path=path;expectedBytes=bytes;expectedSHA256=sha;self.purpose=purpose}
    package static func nativeFixture(path:URL,expectedBytes:Int?,expectedSHA256:String?,purpose:VocabNativeFixture)throws->Self {
        try runtimeRequire(path.isFileURL && path.path.hasPrefix("/"),"absolute native vocab file")
        let absent=expectedBytes == nil && expectedSHA256 == nil
        try runtimeRequire((purpose == .openFailure && absent) ||
            (expectedBytes.map{purpose == .byteLimit ? $0 == 12*1024*1024+1:($0 >= 0 && $0 <= 12*1024*1024)} == true && expectedSHA256.map{$0.count == 64 && $0.allSatisfy{"0123456789abcdef".contains($0)}} == true),"native vocab exact identity")
        return Self(path:path,bytes:expectedBytes,sha:expectedSHA256,purpose:purpose)
    }
    package var isNativeFixture:Bool {purpose != nil}
    package var diagnostic:JSONValue {.object(["path":.string(path.path),"expected_bytes":expectedBytes.map(JSONValue.integer) ?? .null,
        "expected_sha256":expectedSHA256.map(JSONValue.string) ?? .null,"native_purpose":purpose.map{.string($0.rawValue)} ?? .null])}
}
package enum ToolFailureCode:String,Sendable {
    case argumentOutOfRange="ToolArgumentOutOfRange",addedTokenUnsupported="ToolAddedTokenUnsupported",tokenMissing="ToolTokenMissing"
    case openFailure="ToolOpenFailure",readFailure="ToolReadFailure",schemaFailure="ToolSchemaFailure",duplicateMapping="ToolDuplicateMapping",budgetExceeded="ToolBudgetExceeded"
}
package enum VocabParseResult:Sendable {
    case symbol(utf8:Data)
    case sourceFailure(ToolFailureCode,detail:JSONValue)
    case readFailure
    case infrastructure(detail:JSONValue)
}
package final class ToolInvocation:Sendable {
    package let toolCallID:String,serviceID:String,token:UInt64,file:VocabFileCapability
    let caller:Caller,scope:ScopeHandle,site:String,response:ChildResponseEvidence
    init(id:String,argument:UInt64,response:ChildResponseEvidence,caller:Caller,scope:ScopeHandle,site:String,file:VocabFileCapability){
        toolCallID=id;serviceID=response.invocation.serviceID;token=argument;self.file=file
        self.caller=caller;self.scope=scope;self.site=site;self.response=response
    }
    package var diagnostic:JSONValue {.object(["tool_call_id":.string(toolCallID),"service_id":.string(serviceID),"token":.unsigned(token),
        "tool":.string("tokenizer.vocab_symbol"),"response_evidence":response.diagnostic,"file":file.diagnostic,"caller":caller.json,"scope":RuntimeValue.scope(scope).diagnostic,"site":.string(site)])}
}
package protocol VocabSymbolTool:Sendable {func perform(_ invocation:ToolInvocation,root:ToolReadRoot)->ToolCallOutcome}
enum ToolInfrastructureCode:String,Sendable {
    case authorityMismatch="ToolAuthorityMismatch",closeUnknown="ToolCloseUnknown",generic="ToolInfrastructure"
}
enum ToolDisposition:Sendable {case value(RuntimeValue),source(ToolFailureCode,JSONValue),infrastructure(ToolInfrastructureCode,JSONValue)}
package final class ToolCallOutcome:Sendable {
    let root:ToolReadRoot,disposition:ToolDisposition,receipt:JSONValue
    init(root:ToolReadRoot,disposition:ToolDisposition,receipt:JSONValue){self.root=root;self.disposition=disposition;self.receipt=receipt}
    package var diagnostic:JSONValue {
        let result:JSONValue
        switch disposition {case .value(let v):result = .object(["kind":.string("ok"),"value":v.diagnostic])
        case .source(let c,let d):result = .object(["kind":.string("source-failure"),"code":.string(c.rawValue),"detail":d])
        case .infrastructure(let code,let d):result = .object(["kind":.string("infrastructure"),"code":.string(code.rawValue),"detail":d])}
        return .object(["result":result,"receipt":receipt])
    }
}
private struct ToolReadStop:Error {let text:String}
private struct NativeToolCloseEvidenceLoss:Error {
    let text="native-only fault after original FileHandle.close returned, before close evidence acceptance"
}

/// The registered Core root, rather than the parser, owns the actual descriptor.
/// No filesystem operation or parser invocation occurs under its state mutex.
package final class ToolReadRoot:Sendable {
    private struct State {
        var phase="registered",claimed=false,finished=false,opened=false,openReturned=false,closed=false,authority=false,readCompleted=false,closeAttempted=false
        var handle:FileHandle?,descriptor:Int32?,dataBytes:Int?,dataSHA:String?
        var authorityMismatch=false,nativeCloseReturnedNS:UInt64?
        var sourceError:ToolFailureCode?,firstError:JSONValue = .null,closeError:JSONValue = .null
        var rows:[JSONValue]=[],receipt:JSONValue = .null,resolvedPath:String?
    }
    let invocation:ToolInvocation
    private let handleIdentity=UUID().uuidString
    private let state=Mutex(State()),start=DispatchTime.now().uptimeNanoseconds
    private let byteLimit=12*1024*1024,budgetNS:UInt64=2_000_000_000
    init(_ invocation:ToolInvocation){self.invocation=invocation}
    private func row(_ name:String,_ detail:JSONValue = .null)->JSONValue {.object(["event":.string(name),"capture_ns":.string(String(DispatchTime.now().uptimeNanoseconds)),"detail":detail])}
    private func failure(_ error:any Error)->JSONValue {
        let original=error as NSError,underlying=original.userInfo[NSUnderlyingErrorKey] as? NSError
        return .object(["type":.string(String(reflecting:type(of:error))),"description":.string(String(describing:error)),
            "domain":.string(original.domain),"code":.integer(original.code),
            "underlying":underlying.map{.object(["domain":.string($0.domain),"code":.integer($0.code),"description":.string($0.localizedDescription)])} ?? .null,
            "native_evidence_delivery_fault":.bool(error is NativeToolCloseEvidenceLoss)])
    }
    private func nativeCloseObservation(_ stamp:UInt64?)->JSONValue {
        stamp.map{.object(["actual_close_returned":.bool(true),"capture_ns":.string(String($0)),
            "boundary":.string("after-original-close-before-evidence-acceptance"),"physical_close_failure_claimed":.bool(false)])} ?? .null
    }
    private func saveError(_ detail:JSONValue,source:ToolFailureCode?){state.withLock{s in if s.firstError == .null {s.firstError=detail;s.sourceError=source}}}
    package func readAndClose()throws->Data {
        try state.withLock{s in
            try runtimeRequire(!s.claimed && !s.finished,"tool read called twice")
            s.claimed=true;s.phase="opening";s.rows.append(row("tool.open-call"))
        }
        let handle:FileHandle
        let resolved=invocation.file.path.resolvingSymlinksInPath().path
        state.withLock{$0.resolvedPath=resolved}
        do {handle=try FileHandle(forReadingFrom:invocation.file.path)}
        catch {
            saveError(failure(error),source:.openFailure)
            state.withLock{s in s.phase="open-failed";s.openReturned=true;s.rows.append(row("tool.open-failed",s.firstError))}
            throw error
        }
        state.withLock{s in s.handle=handle;s.opened=true;s.openReturned=true;s.descriptor=handle.fileDescriptor;s.phase="reading";s.rows.append(row("tool.opened",.integer(Int(handle.fileDescriptor))))}
        var bytes=Data()
        do {
            guard invocation.file.expectedBytes != nil && invocation.file.expectedSHA256 != nil else {throw ToolReadStop(text:"native open-failure file unexpectedly opened")}
            while true {
                if DispatchTime.now().uptimeNanoseconds-start >= budgetNS {
                    saveError(.string("tool IO budget exceeded"),source:.budgetExceeded);throw ToolReadStop(text:"tool budget")
                }
                let chunk=try handle.read(upToCount:min(65_536,byteLimit+1-bytes.count)) ?? Data()
                if chunk.isEmpty {state.withLock{$0.readCompleted=true};break}
                bytes.append(chunk)
                if bytes.count > byteLimit {saveError(.string("tool file byte limit"),source:.budgetExceeded);throw ToolReadStop(text:"tool byte limit")}
            }
        } catch {
            // A capability mismatch is infrastructure. An actual FileHandle
            // read error is an ordinary tool failure, with its original cause.
            saveError(failure(error),source:error is ToolReadStop ? nil:.readFailure)
        }
        state.withLock{s in s.closeAttempted=true;s.phase="closing";s.rows.append(row("tool.close-call"))}
        do {
            try handle.close()
            if invocation.file.purpose == .closeUnknown {
                // The OS close has really returned. Only its evidence delivery
                // is interrupted; the retained original is never closed again.
                let stamp=DispatchTime.now().uptimeNanoseconds
                state.withLock{s in s.nativeCloseReturnedNS=stamp;s.rows.append(row("tool.native-close-returned",nativeCloseObservation(stamp)))}
                throw NativeToolCloseEvidenceLoss()
            }
            state.withLock{s in s.closed=true;s.handle=nil;s.phase="closed";s.rows.append(row("tool.closed"))}
        } catch {
            state.withLock{s in s.closeError=failure(error);s.phase="close-unknown";s.rows.append(row("tool.close-unknown",s.closeError))}
        }
        let sha=digest(bytes)
        state.withLock{s in
            s.dataBytes=bytes.count;s.dataSHA=sha
            if s.firstError == .null {
                s.authority=bytes.count == invocation.file.expectedBytes && sha == invocation.file.expectedSHA256
                if !s.authority {s.authorityMismatch=true;s.firstError = .string("vocab file authority mismatch")}
            }
            s.rows.append(row("tool.read-return",.object(["bytes":.integer(bytes.count),"sha256":.string(sha),"authority":.bool(s.authority)])))
        }
        let okay=state.withLock{$0.firstError == .null && $0.closeError == .null && $0.closed && $0.authority}
        guard okay else {throw ToolReadStop(text:"tool original read/authority/close failed; see root")}
        return bytes
    }
    package func finish(_ result:VocabParseResult)->ToolCallOutcome {
        let saved:State?=state.withLock{s in
            guard !s.finished else{return nil};s.finished=true;s.phase="sealing-receipt";return s
        }
        guard let saved else {return ToolCallOutcome(root:self,disposition:.infrastructure(.generic,.string("tool finish repeated")),receipt:state.withLock{$0.receipt})}
        // Hashing/base64 and JSON construction are deliberately outside the
        // state lock; watchdog snapshots never wait for parser/serialization.
        var disposition:ToolDisposition,symbolFields:[String:JSONValue]=[
            "symbol_utf8_base64":.null,"symbol_bytes":.null,"symbol_sha256":.null,"digest_words_decimal":.null]
        let noncreated=saved.openReturned && !saved.opened && saved.handle == nil
        if !saved.claimed || (!noncreated && !saved.closed) || saved.closeError != .null {
            disposition = .infrastructure(saved.opened ? .closeUnknown:.generic,.object(["reason":.string("tool IO not actually closed/noncreated"),"original":saved.firstError,"close_error":saved.closeError]))
        } else if saved.firstError != .null {
            if let code=saved.sourceError {disposition = .source(code,saved.firstError)} else {disposition = .infrastructure(saved.authorityMismatch ? .authorityMismatch:.generic,saved.firstError)}
        } else if !saved.authority {disposition = .infrastructure(.generic,.string("tool authority absent"))}
        else {
            switch result {
            case .symbol(let bytes):
                if invocation.token > 151_642 || String(data:bytes,encoding:.utf8) == nil {disposition = .infrastructure(.generic,.string("parser symbol outside ordinary domain/invalid UTF8"))}
                else {
                    let hash=Array(SHA256.hash(data:bytes));var words:[UInt64]=[]
                    for offset in stride(from:0,to:32,by:8) {var word:UInt64=0;for byte in hash[offset..<offset+8] {word=(word << 8)|UInt64(byte)};words.append(word)}
                    disposition = .value(.pair(.nat(UInt64(bytes.count)),.pair(.word(words[0]),.pair(.word(words[1]),.pair(.word(words[2]),.word(words[3]))))))
                    symbolFields=["symbol_utf8_base64":.string(bytes.base64EncodedString()),"symbol_bytes":.integer(bytes.count),
                        "symbol_sha256":.string(hash.map{String(format:"%02x",$0)}.joined()),"digest_words_decimal":.array(words.map{.string(String($0))})]
                }
            case .sourceFailure(let code,let detail):
                let permitted:Bool
                switch code {case .argumentOutOfRange:permitted=invocation.token >= 151_936
                case .addedTokenUnsupported:permitted=(151_643...151_668).contains(invocation.token)
                case .tokenMissing:permitted=(151_669...151_935).contains(invocation.token)
                case .schemaFailure,.duplicateMapping:permitted=true
                case .budgetExceeded:permitted=DispatchTime.now().uptimeNanoseconds-start >= budgetNS
                case .openFailure,.readFailure:permitted=false}
                disposition = permitted ? .source(code,detail):.infrastructure(.generic,.string("parser manufactured IO/domain fault"))
            case .readFailure:disposition = .infrastructure(.generic,.string("readFailure without original IO failure"))
            case .infrastructure(let detail):disposition = .infrastructure(.generic,detail)
            }
        }
        func resultFields(_ value:ToolDisposition)->(JSONValue,JSONValue) {
            switch value {case .value:return(.string("ok"),.null)
            case .source(let code,let detail):return(.string("source-failure"),.object(["code":.string(code.rawValue),"detail":detail]))
            case .infrastructure(let code,let detail):return(.string("infrastructure"),.object(["code":.string(code.rawValue),"detail":detail]))}
        }
        var fields:[String:JSONValue]=[
            "schema":.string("src1b-tool-receipt-v1"),"tool_call":.string(invocation.toolCallID),"service_id":.string(invocation.serviceID),
            "caller":invocation.caller.json,"site":.string(invocation.site),"argument_token":.string(String(invocation.token)),
            "response_evidence":invocation.response.diagnostic,
            "authority":.object(["path":.string(invocation.file.path.path),"resolved_path":saved.resolvedPath.map(JSONValue.string) ?? .null,
                "bytes":saved.dataBytes.map(JSONValue.integer) ?? .null,"sha256":saved.dataSHA.map(JSONValue.string) ?? .null,"matched":.bool(saved.authority)]),
            "json_path":.array([.string("model"),.string("vocab")]),"representation":.string("bpe-bytelevel-vocab-key-utf8"),"decode_performed":.bool(false),
            "io":.object(["opened":.bool(saved.opened),"open_returned":.bool(saved.openReturned),"noncreated":.bool(noncreated),
                "read_completed":.bool(saved.readCompleted),"close_attempted":.bool(saved.closeAttempted),"closed":.bool(saved.closed),
                "handle_identity":saved.opened ? .object(["id":.string(handleIdentity),"descriptor":saved.descriptor.map{.integer(Int($0))} ?? .null]):.null]),
            "work_budget_ns":.string(String(budgetNS)),"original_io_error":saved.firstError,"original_close_error":saved.closeError,
            "native_close_observation":nativeCloseObservation(saved.nativeCloseReturnedNS)]
        fields.merge(symbolFields){_,new in new}
        let initialResult=resultFields(disposition);fields["outcome"]=initialResult.0;fields["failure"]=initialResult.1
        // Seal all potentially large values first, then sample the final work
        // gate. A miss is sticky; no returned success uses an earlier timestamp.
        state.withLock{s in s.receipt = .object(fields);s.phase="receipt-sealed";s.rows.append(row("tool.receipt-sealed"))}
        let finished=DispatchTime.now().uptimeNanoseconds
        var within=finished-start < budgetNS
        if !within,case .value=disposition {disposition = .source(.budgetExceeded,.string("tool receipt sealing budget"))}
        fields["clock"] = .object(["domain":.string("swift-dispatch-uptime-ns"),"started_ns":.string(String(start)),"finished_ns":.string(String(finished)),"elapsed_ns":.string(String(finished-start))])
        fields["within_work_budget"] = .bool(within)
        let resultPair=resultFields(disposition);fields["outcome"]=resultPair.0;fields["failure"]=resultPair.1
        return state.withLock{s in
            s.receipt = .object(fields);s.phase="finished";s.rows.append(row("tool.finished"))
            // Sample after acquiring the final receipt lock and saving the
            // original fields. No further lock acquisition follows this gate.
            let last=DispatchTime.now().uptimeNanoseconds
            fields["last_gate_ns"] = .string(String(last))
            if last-start >= budgetNS {
                within=false
                if case .value=disposition {disposition = .source(.budgetExceeded,.string("tool final receipt gate"))}
                let pair=resultFields(disposition);fields["outcome"]=pair.0;fields["failure"]=pair.1;fields["within_work_budget"] = .bool(false)
            }
            // Only bounded final clock/status metadata and immutable return
            // construction follow this observational, non-preemptive gate.
            s.receipt = .object(fields)
            return ToolCallOutcome(root:self,disposition:disposition,receipt:.object(fields))
        }
    }
    var actuallyClosed:Bool {state.withLock{$0.claimed && $0.closed && $0.closeError == .null && $0.handle == nil}}
    var resourceSafe:Bool {state.withLock{$0.claimed && $0.openReturned && (!$0.opened || $0.closed) && $0.closeError == .null && $0.handle == nil}}
    package func snapshot()->JSONValue {state.withLock{s in .object(["tool_call_id":.string(invocation.toolCallID),"phase":.string(s.phase),
        "opened":.bool(s.opened),"closed":.bool(s.closed),"descriptor":s.descriptor.map{.integer(Int($0))} ?? .null,"actual_handle_retained":.bool(s.handle != nil),
        "authority":.bool(s.authority),"finished":.bool(s.finished),"first_error":s.firstError,"close_error":s.closeError,
        "native_close_observation":nativeCloseObservation(s.nativeCloseReturnedNS),"receipt":s.receipt,"events":.array(s.rows)])}}
}
