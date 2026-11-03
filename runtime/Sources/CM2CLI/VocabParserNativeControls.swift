import Foundation
import RuntimeCore

/// Finite parser-only controls. Calling this function performs in-memory lookup;
/// merely loading this file does not invoke any candidate. The future native
/// runner owns execution, raw output, finite budget and actual exit evidence.
/// No FileHandle, ToolReadRoot, child service, checked source or model is used.
func runVocabParserNativeControls() -> JSONValue {
    let controls = vocabParserNativeCases()
    var rows: [JSONValue] = []
    let populationValid = controls.count == 44 && Set(controls.map(\.id)).count == 44
    var allPassed = populationValid
    for control in controls {
        let actual: JSONValue
        let errorRecord: JSONValue
        let passed: Bool
        do {
            let symbol = try VocabSymbolParser.lookup(control.input, token: control.token)
            actual = .object(["outcome": .string("symbol"),
                "symbol_utf8_base64": .string(symbol.base64EncodedString()),
                "symbol_bytes": .number(String(symbol.count))])
            errorRecord = .null
            if case .symbol(let expected) = control.expected { passed = symbol == expected }
            else { passed = false }
        } catch let error as VocabSymbolParseFailure {
            actual = .object(["outcome": .string("failure"), "code": .string(error.code.rawValue)])
            errorRecord = .object(["kind": .string("VocabSymbolParseFailure"),
                "code": .string(error.code.rawValue), "diagnostic": error.diagnostic])
            if case .failure(let code, let message) = control.expected {
                passed = error.code.rawValue == code && error.message == message
            } else { passed = false }
        } catch {
            actual = .object(["outcome": .string("unexpected-error")])
            errorRecord = .object(["kind": .string(String(reflecting: type(of: error))),
                                   "message": .string(String(describing: error))])
            passed = false
        }
        allPassed = allPassed && passed
        rows.append(.object([
            "id": .string(control.id), "group": .string(control.group),
            "purpose": .string(control.purpose), "input_base64": .string(control.input.base64EncodedString()),
            "input_bytes": .number(String(control.input.count)), "argument_token": .string(String(control.token)),
            "expected": control.expected.json, "actual": actual, "error": errorRecord, "passed": .bool(passed)
        ]))
    }
    return .object([
        "schema": .string("src1b-vocab-parser-native-controls-v1"),
        "layer": .string("in-memory-VocabSymbolParser.lookup-only"),
        "allPassed": .bool(allPassed), "population_valid": .bool(populationValid),
        "case_count": .number(String(controls.count)), "cases": .array(rows),
        "groups": .array(["ordinary-added-missing-domain", "byte-key-identity", "unicode", "syntax-and-ID", "parser-depth-bound"].map { .string($0) }),
        "file_io_performed": .bool(false), "production_authority_loaded": .bool(false),
        "tool_root_or_receipt_exercised": .bool(false), "checked_parent_or_child_executed": .bool(false),
        "model_executed": .bool(false),
        "limits": .array([
            .string("Compatible small synthetic JSON documents, not the fixed production tokenizer authority or its full population."),
            .string("Expected UTF8 bytes are explicit byte arrays; no normalization or JSON dictionary reconstruction supplies the oracle."),
            .string("Negative cases isolate one violation. Exact typed code/message are compared; original diagnostic offsets are retained as observed evidence."),
            .string("Depth128/129 are exercised as parser implementation bounds; 12MiB/2million-node limits and parser/whole-tool timing are not exercised by this finite set."),
            .string("No actual open/read/close, authority failure, duplicate root invocation, foreign root, late task join or checked source-to-tool dataflow is established here."),
            .string("This function does not run the root acceptance protocol; native runner must save all rows and verify its own final budget and actual outer exit.")
        ])
    ])
}

private enum VocabNativeExpectation {
    case symbol(Data)
    case failure(String, String)
    var json: JSONValue {
        switch self {
        case .symbol(let bytes):
            return .object(["outcome": .string("symbol"),
                "symbol_utf8_base64": .string(bytes.base64EncodedString()),
                "symbol_bytes": .number(String(bytes.count))])
        case .failure(let code, let message):
            return .object(["outcome": .string("failure"), "code": .string(code), "message": .string(message)])
        }
    }
}

private struct VocabNativeCase {
    let id: String
    let group: String
    let purpose: String
    let input: Data
    let token: UInt64
    let expected: VocabNativeExpectation
}

private func vocabParserNativeCases() -> [VocabNativeCase] {
    var cases: [VocabNativeCase] = []
    // Input grammar is deliberately written as raw text/bytes, not decoded and
    // re-encoded through a dictionary that could discard duplicate keys.
    func document(_ vocab: String, added: String = "[]", metadata: String? = nil) -> Data {
        let extra = metadata.map { ",\"metadata\":" + $0 } ?? ""
        return Data(("{\"model\":{\"vocab\":{" + vocab + "}},\"added_tokens\":" + added + extra + "}").utf8)
    }
    func rawSymbol(_ bytes: [UInt8]) -> Data {
        var data = Data("{\"model\":{\"vocab\":{\"".utf8)
        data.append(contentsOf: bytes)
        data.append(Data("\":0}},\"added_tokens\":[]}".utf8))
        return data
    }
    func success(_ id: String, _ group: String, _ purpose: String,
                 _ input: Data, _ token: UInt64, _ expected: [UInt8]) {
        cases.append(VocabNativeCase(id: id, group: group, purpose: purpose,
            input: input, token: token, expected: .symbol(Data(expected))))
    }
    func failure(_ id: String, _ group: String, _ purpose: String,
                 _ input: Data, _ token: UInt64, _ code: String, _ message: String) {
        cases.append(VocabNativeCase(id: id, group: group, purpose: purpose,
            input: input, token: token, expected: .failure(code, message)))
    }

    func malformed(_ id: String, _ group: String, _ purpose: String,
                   _ input: Data, _ message: String) {
        failure(id, group, purpose, input, 0, "ToolSchemaFailure", message)
    }

    let domain = "ordinary-added-missing-domain"
    let small = document(#""A":0,"B":1,"Z":151642"#,
                         added: #"[{"id":151643,"content":"<added>"}]"#)
    success("D01", domain, "Strict unsigned zero is an ordinary ID.", small, 0, [0x41])
    success("D02", domain, "A second ordinary ID selects its own exact bytes.", small, 1, [0x42])
    success("D03", domain, "Ordinary upper endpoint151642 is permitted.", small, 151642, [0x5A])
    failure("D04", domain, "Actual added ID is unsupported; no fallback.", small, 151643,
            "ToolAddedTokenUnsupported", "token 151643 is an added token, not an ordinary BPE symbol")
    failure("D05", domain, "In-domain ID absent from ordinary map remains missing.", small, 151669,
            "ToolTokenMissing", "token 151669 absent from ordinary vocabulary")
    failure("D06", domain, "Argument outside model domain is rejected without modulo.", small, 151936,
            "ToolArgumentOutOfRange", "token 151936 outside model vocabulary")

    let keys = "byte-key-identity"
    let equivalent = document(#""\u00e9":0,"e\u0301":1"#)
    success("K01", keys, "NFC key preserves its original two UTF8 bytes.", equivalent, 0, [0xC3, 0xA9])
    success("K02", keys, "NFD key remains distinct despite String canonical equivalence.", equivalent, 1, [0x65, 0xCC, 0x81])
    failure("K03", keys, "Literal and escaped ASCII byte key are duplicates.", document(#""A":0,"\u0041":1"#), 0,
            "ToolDuplicateMapping", "duplicate unescaped JSON byte key")
    failure("K04", keys, "Slash escape and literal slash identify the same bytes.", document(#""/":0,"\/":1"#), 0,
            "ToolDuplicateMapping", "duplicate unescaped JSON byte key")
    failure("K05", keys, "Backslash escape and Unicode escape identify the same bytes.", document(#""\\":0,"\u005c":1"#), 0,
            "ToolDuplicateMapping", "duplicate unescaped JSON byte key")
    failure("K06", keys, "Different ordinary keys cannot share one ID.", document(#""A":0,"B":0"#), 0,
            "ToolDuplicateMapping", "duplicate ordinary vocabulary ID 0")
    failure("K07", keys, "Added ID duplication is checked independently of content.",
            document(#""A":0"#, added: #"[{"id":151643,"content":"x"},{"id":151643,"content":"y"}]"#), 0,
            "ToolDuplicateMapping", "duplicate added token ID or symbol")
    failure("K08", keys, "Added content duplication is checked independently of ID.",
            document(#""A":0"#, added: #"[{"id":151643,"content":"x"},{"id":151644,"content":"x"}]"#), 0,
            "ToolDuplicateMapping", "duplicate added token ID or symbol")
    failure("K09", keys, "An added symbol cannot overlap ordinary bytes.",
            document(#""A":0"#, added: #"[{"id":151643,"content":"A"}]"#), 0,
            "ToolDuplicateMapping", "duplicate added token ID or symbol")

    let unicode = "unicode"
    success("U01", unicode, "Literal multibyte BMP and supplementary UTF8 remain exact.",
            document(#""π😀":0"#), 0, [0xCF, 0x80, 0xF0, 0x9F, 0x98, 0x80])
    success("U02", unicode, "Valid surrogate pair becomes its exact scalar UTF8.",
            document(#""\uD83D\uDE00":0"#), 0, [0xF0, 0x9F, 0x98, 0x80])
    success("U03", unicode, "Escaped controls/quote/backslash/slash are decoded, not normalized.",
            document(#""\u0000\b\f\n\r\t\"\\\/":0"#), 0, [0, 8, 12, 10, 13, 9, 34, 92, 47])
    malformed("U04", unicode, "Lone high surrogate has no paired scalar.", document(#""\uD800":0"#), "unpaired high surrogate")
    malformed("U05", unicode, "Lone low surrogate cannot start a scalar.", document(#""\uDC00":0"#), "unpaired low surrogate")
    malformed("U06", unicode, "High surrogate followed by a non-low scalar is rejected.", document(#""\uD800\u0041":0"#), "invalid low surrogate")
    malformed("U07", unicode, "Unicode escape digits are strictly hexadecimal.", document(#""\u00G0":0"#), "invalid unicode escape")
    malformed("U08", unicode, "Raw control bytes are not legal JSON string content.", rawSymbol([0x01]), "unescaped JSON control byte")
    malformed("U09", unicode, "Overlong UTF8 is not repaired.", rawSymbol([0xC0, 0xAF]), "invalid UTF-8 string")
    malformed("U10", unicode, "Truncated multibyte UTF8 is not repaired.", rawSymbol([0xE2, 0x82]), "invalid UTF-8 string")

    let syntax = "syntax-and-ID"
    var trailing = document(#""A":0"#); trailing.append(Data(" true".utf8))
    malformed("J01", syntax, "Exactly one complete JSON document is consumed.", trailing, "trailing JSON bytes")
    malformed("J02", syntax, "Trailing object comma is not accepted.", document(#""A":0,"#), "expected JSON string")
    malformed("J03", syntax, "Object member colon is mandatory.", document(#""A" 0"#), "expected object colon")
    malformed("J04", syntax, "Object value cannot be omitted.", document(#""A":"#), "invalid JSON value")
    malformed("J05", syntax, "Boolean literal spelling is exact.", document(#""A":tru"#), "invalid JSON literal")
    malformed("J06", syntax, "Truncated string is not silently completed.", Data(#"{"model":{"vocab":{"A"#.utf8), "unterminated JSON string")
    malformed("J07", syntax, "A leading zero cannot prefix more integer digits.", document(#""A":01"#), "number with leading zero")
    malformed("J08", syntax, "Fraction requires digits after decimal point.", document(#""A":1."#), "empty fractional part")
    malformed("J09", syntax, "Exponent requires digits after optional sign.", document(#""A":1e+"#), "empty exponent")
    malformed("J10", syntax, "A bare minus is not a complete number.", Data("-".utf8), "truncated number")
    let idMessage = "/model/vocab: ID must be a nonnegative JSON integer"
    malformed("I01", syntax, "Bool must not coerce to vocabulary ID.", document(#""A":true"#), idMessage)
    malformed("I02", syntax, "A fractional spelling is invalid even for integral value.", document(#""A":0.0"#), idMessage)
    malformed("I03", syntax, "An exponent spelling is invalid for ID.", document(#""A":0e0"#), idMessage)
    malformed("I04", syntax, "Negative-zero spelling is not an unsigned ID.", document(#""A":-0"#), idMessage)
    malformed("I05", syntax, "A string does not coerce to an integer ID.", document(#""A":"0""#), idMessage)
    malformed("I06", syntax, "Checked UInt64 accumulation rejects overflow.", document(#""A":18446744073709551616"#), "/model/vocab: ID overflow")
    success("I07", syntax, "Valid fraction/exponent metadata remains legal JSON outside ID fields.",
            document(#""A":0"#, metadata: "[-1.25,1e+2,0.0]"), 0, [0x41])

    let bounds = "parser-depth-bound"
    // Top object has depth0; metadata starts at1. 127 nested arrays put their
    // null value at128; the otherwise identical 128-array input reaches129.
    let depth128 = String(repeating: "[", count: 127) + "null" + String(repeating: "]", count: 127)
    let depth129 = String(repeating: "[", count: 128) + "null" + String(repeating: "]", count: 128)
    success("R01", bounds, "Declared depth128 endpoint is retained as an implementation bound.",
            document(#""A":0"#, metadata: depth128), 0, [0x41])
    malformed("R02", bounds, "Next nesting level is rejected before lookup.",
            document(#""A":0"#, metadata: depth129), "JSON depth or node bound")
    return cases
}
