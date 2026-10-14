// Siri voices for claude_phone/siri.py. Run with the `swift` interpreter, not compiled: macOS gives Siri voices
// only to Apple-signed processes, and an ad-hoc signed build (or Python) sees none.
//   swift siri.swift --list               identifier, name and language of each Siri voice, tab separated
//   swift siri.swift VOICE OUT < text     speak text into OUT (CAF). VOICE: identifier, name, language or ""
import AVFoundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

let siri = AVSpeechSynthesisVoice.speechVoices().filter { $0.identifier.hasPrefix("com.apple.siri.") }
let args = CommandLine.arguments
if args.count == 2 && args[1] == "--list" {
    for v in siri { print("\(v.identifier)\t\(v.name)\t\(v.language)") }
    exit(0)
}
guard args.count == 3 else { fail("usage: siri.swift --list | VOICE OUT < text") }

// Empty VOICE: the first Siri voice in the Mac's language, else the first one.
let want = args[1].lowercased()
let match = want.isEmpty
    ? siri.first { $0.language == AVSpeechSynthesisVoice.currentLanguageCode() } ?? siri.first
    : siri.first { [$0.identifier, $0.name, $0.language].map { $0.lowercased() }.contains(want) }
guard let voice = match else {
    fail(siri.isEmpty
        ? "no Siri voice installed (System Settings → Accessibility → Spoken Content → System Voice → Manage Voices)"
        : "no Siri voice \"\(args[1])\"; `phone voices` lists them")
}

let text = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8) ?? ""
let utterance = AVSpeechUtterance(string: text)
utterance.voice = voice
let synth = AVSpeechSynthesizer()
var file: AVAudioFile?
var frames: AVAudioFrameCount = 0
var done = false
synth.write(utterance) { buf in
    guard !done, let pcm = buf as? AVAudioPCMBuffer else { return }
    if pcm.frameLength == 0 { done = true; return }  // an empty buffer marks the end
    do {
        if file == nil {
            file = try AVAudioFile(forWriting: URL(fileURLWithPath: args[2]), settings: pcm.format.settings,
                                   commonFormat: pcm.format.commonFormat, interleaved: pcm.format.isInterleaved)
        }
        try file!.write(from: pcm)
        frames += pcm.frameLength
    } catch {
        fail("\(error)")
    }
}
let deadline = Date().addingTimeInterval(50)
while !done && Date() < deadline { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
file = nil  // closes the file
if frames == 0 { fail("no audio") }
