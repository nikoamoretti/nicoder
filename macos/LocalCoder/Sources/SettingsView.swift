import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Picker("Model", selection: $model.selectedModelId) {
                ForEach(model.models, id: \.id) { item in
                    Text(item.available ? item.name : "\(item.name) (not installed)")
                        .tag(item.id)
                }
            }
            .disabled(model.running || model.models.isEmpty)
            .onChange(of: model.selectedModelId) { _, newValue in
                model.selectModel(newValue)
            }
            if let note = model.selectedModelNote {
                Text(note)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Picker("Engine", selection: $model.engineName) {
                Text("General (local apply)").tag("general")
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
            Text("Bonsai 2 27B is the preferred model. Keep it selected unless it is too slow. Choosing a model is only the first step. Close this window, open or create a project, then type what you want.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Check this Mac again") { model.refreshReadiness() }
            Button("Done") { model.showSettings = false }
                .keyboardShortcut(.defaultAction)
        }
        .padding(20)
        .navigationTitle("Advanced")
    }
}
