import Foundation
import IOBluetooth

final class Delegate: NSObject, IOBluetoothRFCOMMChannelDelegate {
    var rx = Data()

    func rfcommChannelData(_ rfcommChannel: IOBluetoothRFCOMMChannel!, data dataPointer: UnsafeMutableRawPointer!, length dataLength: Int) {
        let d = Data(bytes: dataPointer, count: dataLength)
        rx.append(d)
        print("rx \(dataLength): \(d.map { String(format: "%02x", $0) }.joined(separator: " "))")
    }

    func rfcommChannelOpenComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, status error: IOReturn) {
        print("openComplete status=0x\(String(error, radix: 16))")
    }

    func rfcommChannelClosed(_ rfcommChannel: IOBluetoothRFCOMMChannel!) {
        print("closed")
    }

    func rfcommChannelWriteComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, refcon: UnsafeMutableRawPointer!, status error: IOReturn) {
        print("writeComplete status=0x\(String(error, radix: 16))")
    }

    func rfcommChannelWriteComplete(_ rfcommChannel: IOBluetoothRFCOMMChannel!, refcon: UnsafeMutableRawPointer!, status error: IOReturn, bytesWritten length: Int) {
        print("writeComplete status=0x\(String(error, radix: 16)) bytes=\(length)")
    }

    func rfcommChannelQueueSpaceAvailable(_ rfcommChannel: IOBluetoothRFCOMMChannel!) {
        print("queueSpace")
    }
}

func hexData(_ s: String) -> Data {
    var out = Data()
    var chars = Array(s.replacingOccurrences(of: " ", with: ""))
    while chars.count >= 2 {
        let byte = String(chars.removeFirst()) + String(chars.removeFirst())
        out.append(UInt8(byte, radix: 16)!)
    }
    return out
}

let address = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "B1:21:81:B1:F0:84"
guard let dev = IOBluetoothDevice(addressString: address) else {
    fatalError("device not found: \(address)")
}

print("device \(dev.name ?? "?") \(dev.addressString ?? "?") connected=\(dev.isConnected())")

print("SDP query...")
let sdp = dev.performSDPQuery(nil)
print("performSDPQuery ret=0x\(String(sdp, radix: 16))")
RunLoop.current.run(until: Date().addingTimeInterval(3.0))

let services = (dev.services as? [IOBluetoothSDPServiceRecord]) ?? []
print("services \(services.count)")
var channels = Set<UInt8>()
for (idx, svc) in services.enumerated() {
    var cid = BluetoothRFCOMMChannelID(0)
    let ret = svc.getRFCOMMChannelID(&cid)
    let name = svc.getServiceName() ?? "?"
    print("svc[\(idx)] name=\(name) rfcommRet=0x\(String(ret, radix: 16)) channel=\(cid)")
    if ret == kIOReturnSuccess && cid > 0 { channels.insert(cid) }
}
if channels.isEmpty { channels = Set([1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30]) }

let delegate = Delegate()
let prelude = [hexData("0103009fa20002"), hexData("010400bd31f20002")]

for ch in channels.sorted() {
    print("\ntrying RFCOMM channel \(ch)")
    var rf: IOBluetoothRFCOMMChannel?
    let ret = dev.openRFCOMMChannelSync(&rf, withChannelID: BluetoothRFCOMMChannelID(ch), delegate: delegate)
    print("open ret=0x\(String(ret, radix: 16)) channelObj=\(rf != nil)")
    guard ret == kIOReturnSuccess, let chan = rf else { continue }
    RunLoop.current.run(until: Date().addingTimeInterval(0.5))
    for p in prelude {
        var d = p
        let len = UInt16(d.count)
        let w = d.withUnsafeMutableBytes { ptr in
            chan.writeSync(ptr.baseAddress, length: len)
        }
        print("wrote prelude len=\(d.count) ret=0x\(String(w, radix: 16))")
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
    }
    RunLoop.current.run(until: Date().addingTimeInterval(3.0))
    chan.close()
}
