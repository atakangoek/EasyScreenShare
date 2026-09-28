import SwiftUI

struct ContentView: View {
    @StateObject private var browser = PCBrowser()
    @State private var picker = BroadcastPickerController()
    @State private var isStreaming = UIScreen.main.isCaptured
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "iphone.and.arrow.forward")
                .font(.system(size: 64, weight: .light))
                .foregroundStyle(.tint)
            Text("EasyScreenShare")
                .font(.largeTitle.bold())

            status
                .multilineTextAlignment(.center)
                .frame(minHeight: 60)

            Button {
                picker.present()
            } label: {
                Label(isStreaming ? "Stop streaming" : "Start streaming",
                      systemImage: isStreaming ? "stop.circle.fill" : "play.circle.fill")
                    .font(.title3.bold())
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
            }
            .buttonStyle(.borderedProminent)
            .tint(isStreaming ? .red : .accentColor)

            Text("In the sheet that opens, tap **Start Broadcast**. Your screen and app sound go to the PC until you stop — also via the red status pill or Control Center.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Spacer()

            BroadcastPickerView(controller: picker)
                .frame(width: 1, height: 1)
                .opacity(0.01)
                .accessibilityHidden(true)
        }
        .padding(24)
        .onAppear { browser.search() }
        .onReceive(NotificationCenter.default.publisher(for: UIScreen.capturedDidChangeNotification)) { _ in
            isStreaming = UIScreen.main.isCaptured
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active {
                isStreaming = UIScreen.main.isCaptured
                if !isStreaming { browser.search() }
            }
        }
    }

    @ViewBuilder private var status: some View {
        switch browser.status {
        case .searching:
            HStack(spacing: 10) {
                ProgressView()
                Text("Looking for your PC…")
            }
            .foregroundStyle(.secondary)
        case .found(let names):
            VStack(spacing: 4) {
                Label("Ready: \(names[0])", systemImage: "desktopcomputer")
                    .foregroundStyle(.green)
                if names.count > 1 {
                    Text("\(names.count) PCs found (\(names.joined(separator: ", "))). Streaming goes to whichever answers first, so close the receiver on the others.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        case .permissionDenied:
            VStack(spacing: 8) {
                Text("Local Network access is off, so the PC can't be found.")
                Button("Open Settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
            }
        case .notFound:
            VStack(spacing: 8) {
                Text("No PC found. Start EasyScreenShare on the PC, use the same Wi-Fi, and allow it through Windows Firewall on private networks.")
                    .foregroundStyle(.secondary)
                Button("Search again") { browser.search() }
            }
        }
    }
}
