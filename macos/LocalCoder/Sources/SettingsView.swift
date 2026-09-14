import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Picker("Coding profile", selection: $model.profile) {
                Text("d2 — default span editing").tag("d2")
                Text("d1 — exact-text fallback").tag("d1")
            }
            .onChange(of: model.profile) { _, _ in
                model.saveProfile()
            }
            Text("Keep d2 unless you have a reason to use the older exact-text profile. The app will not switch this automatically after a failed request.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Check this Mac again") { model.refreshReadiness() }
        }
        .padding(20)
        .navigationTitle("Advanced")
    }
}
