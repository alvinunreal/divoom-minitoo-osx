import Foundation
import IOBluetooth

final class Delegate: NSObject, IOBluetoothRFCOMMChannelDelegate {
    func rfcommChannelData(_ rfcommChannel: IOBluetoothRFCOMMChannel!, data dataPointer: UnsafeMutableRawPointer!, length dataLength: Int) {
        let d = Data(bytes: dataPointer, count: dataLength)
        print("rx \(dataLength): \(d.map { String(format: "%02x", $0) }.joined(separator: " "))")
    }
    func rfcommChannelOpenComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, status error: IOReturn) { print("openComplete 0x\(String(error, radix: 16))") }
    func rfcommChannelClosed(_ rfcommChannel: IOBluetoothRFCOMMChannel!) { print("closed") }
    func rfcommChannelWriteComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, refcon: UnsafeMutableRawPointer!, status error: IOReturn, bytesWritten length: Int) {
        print("writeComplete 0x\(String(error, radix: 16)) bytes=\(length)")
    }
    func rfcommChannelQueueSpaceAvailable(_ rfcommChannel: IOBluetoothRFCOMMChannel!) { }
}

let address = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "B1:21:81:B1:F0:84"
let channelID = BluetoothRFCOMMChannelID(CommandLine.arguments.count > 2 ? UInt8(CommandLine.arguments[2])! : 1)
let packetPath = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : "captures/mac-send/captured-packets-lenpref.bin"
let delay = CommandLine.arguments.count > 4 ? Double(CommandLine.arguments[4])! : 0.012

guard let dev = IOBluetoothDevice(addressString: address) else { fatalError("device not found") }
let delegate = Delegate()
var channel: IOBluetoothRFCOMMChannel?
print("open \(dev.name ?? "?") channel \(channelID)")
let ret = dev.openRFCOMMChannelSync(&channel, withChannelID: channelID, delegate: delegate)
print("open ret=0x\(String(ret, radix: 16)) obj=\(channel != nil)")
guard ret == kIOReturnSuccess, let ch = channel else { exit(2) }
RunLoop.current.run(until: Date().addingTimeInterval(0.5))

let bytes = try Data(contentsOf: URL(fileURLWithPath: packetPath))
var off = 0
var idx = 0
while off + 2 <= bytes.count {
    let len = Int(bytes[off]) | (Int(bytes[off + 1]) << 8)
    off += 2
    if off + len > bytes.count { fatalError("truncated packet") }
    var pkt = Data(bytes[off..<off+len])
    off += len
    let w = pkt.withUnsafeMutableBytes { ptr in ch.writeSync(ptr.baseAddress, length: UInt16(len)) }
    if idx == 0 || idx == 1 || idx % 10 == 0 || off == bytes.count {
        print("tx \(idx) len=\(len) ret=0x\(String(w, radix: 16))")
    }
    RunLoop.current.run(until: Date().addingTimeInterval(delay))
    idx += 1
}
print("sent packets=\(idx)")
RunLoop.current.run(until: Date().addingTimeInterval(2.0))
ch.close()
