import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Picker("Engine", selection: $model.engineName) {
                Text("General (OpenCode)").tag("general")
                Text("Legacy Python repair").tag("legacy")
            }
            .onChange(of: model.engineName) { _, _ in
                model.saveProfile()
            }
            Picker("Legacy profile", selection: $model.profile) {
                Text("d2 — default span editing").tag("d2")
                Text("d1 — exact-text fallback").tag("d1")
            }
            .onChange(of: model.profile) { _, _ in
                model.saveProfile()
            }
            Text("Ordinary use is the general engine. The older Python-only profiles stay available here and are never switched automatically.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Check this Mac again") { model.refreshReadiness() }
        }
        .padding(20)
        .navigationTitle("Advanced")
    }
}
