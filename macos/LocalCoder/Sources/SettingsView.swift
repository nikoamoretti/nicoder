import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        TabView(selection: $model.settingsTab) {
            Tab("Models", systemImage: "cpu", value: "models") {
                ModelsSettings()
            }
            Tab("OrcaRouter", systemImage: "point.3.filled.connected.trianglepath.dotted", value: "orcarouter") {
                OrcaRouterSettings()
            }
            Tab("Advanced", systemImage: "gearshape.2", value: "advanced") {
                AdvancedSettings()
            }
        }
        .frame(width: 560, height: 440)
    }
}

private struct ModelsSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var browsing = false

    private var selection: Binding<String> {
        Binding(
            get: { model.selectedModelId },
            set: { id in model.selectModel(id) }
        )
    }

    var body: some View {
        Form {
            Section {
                Picker("Model", selection: selection) {
                    ForEach(model.modelGroups, id: \.0) { group, items in
                        Section(group) {
                            ForEach(items) { item in
                                Text(item.provider == "local" && !item.available ? "\(item.name) (not installed)" : item.name)
                                    .tag(item.id)
                            }
                        }
                    }
                }
                .disabled(model.running || model.models.isEmpty)
                if let note = model.selectedModel?.note, !note.isEmpty {
                    Text(note)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } footer: {
                Text("OrcaRouter models run on each provider's servers through OrcaRouter. Nemotron runs on NVIDIA's servers for free. The others run on this Mac.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Button("Browse All OrcaRouter Models…") { browsing = true }
            }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $browsing) {
            ModelBrowser()
                .environmentObject(model)
        }
    }
}

private struct OrcaRouterSettings: View {
    @EnvironmentObject private var model: AppModel
    @State private var connecting = false

    var body: some View {
        Form {
            Section {
                LabeledContent("Status") {
                    if model.orcaConnected {
                        Label("Connected", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    } else {
                        Label("Not connected", systemImage: "circle.dashed")
                            .foregroundStyle(.secondary)
                    }
                }
                if model.orcaConnected {
                    LabeledContent("Key", value: model.orcaWhere.hasPrefix("environment")
                                   ? "From the ORCAROUTER_API_KEY variable" : "In this Mac's Keychain")
                    Button("Disconnect", role: .destructive) { model.disconnectOrcaRouter() }
                        .disabled(model.orcaWhere.hasPrefix("environment"))
                } else {
                    Button("Connect OrcaRouter…") { connecting = true }
                        .buttonStyle(.borderedProminent)
                }
            } footer: {
                Text("Disconnecting removes the key from this Mac only. It stays valid in your OrcaRouter console until you delete it there.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                LabeledContent("Models") {
                    Text(model.routerModels.isEmpty ? "Not loaded yet" : "\(model.routerModels.count) can use Local Coder's tools")
                }
                Link("Open the OrcaRouter console", destination: URL(string: "https://www.orcarouter.ai/console")!)
            }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $connecting) {
            ConnectOrcaRouterView()
                .environmentObject(model)
        }
        .onChange(of: model.orcaConnected) { _, connected in
            if connected { connecting = false }
        }
    }
}

private struct AdvancedSettings: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            Section {
                Picker("Engine for local models", selection: $model.engineName) {
                    Text("General").tag("general")
                    Text("Legacy Python repair").tag("legacy")
                }
                .onChange(of: model.engineName) { _, _ in model.saveProfile() }
                Picker("Legacy profile", selection: $model.profile) {
                    Text("d2: span editing").tag("d2")
                    Text("d1: exact-text fallback").tag("d1")
                }
                .onChange(of: model.profile) { _, _ in model.saveProfile() }
            } footer: {
                Text("Hosted models always use Local Coder's own agent. These settings only matter for models that run on this Mac.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("This Mac") {
                let rows = model.readiness?["rows"] as? [[String: Any]] ?? []
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    LabeledContent {
                        Text(row["detail"] as? String ?? "")
                            .multilineTextAlignment(.trailing)
                            .foregroundStyle((row["ok"] as? Bool ?? false) ? .secondary : Color.orange)
                    } label: {
                        Label((row["name"] as? String ?? "").capitalized,
                              systemImage: (row["ok"] as? Bool ?? false) ? "checkmark.circle" : "exclamationmark.triangle")
                    }
                }
                Button("Check Again") { model.refreshReadiness() }
            }
        }
        .formStyle(.grouped)
    }
}
