import SwiftUI

/// The model switcher in the composer: grouped by where each model runs, with every OrcaRouter model in a
/// submenu per provider.
struct ModelMenu: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Menu {
            ForEach(model.modelGroups, id: \.0) { group, items in
                Section(group) {
                    ForEach(items) { item in
                        Toggle(isOn: binding(for: item)) {
                            Text(item.name)
                            Text(itemSubtitle(item))
                        }
                        .disabled(item.provider == "local" && !item.available)
                    }
                    if group == "OrcaRouter" {
                        providerMenus
                        orcaActions
                    }
                }
            }
            if !model.modelGroups.contains(where: { $0.0 == "OrcaRouter" }) {
                Section("OrcaRouter") {
                    providerMenus
                    orcaActions
                }
            }
            Divider()
            SettingsLink {
                Text("Model Settings…")
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: model.selectedModel?.placeSymbol ?? "cpu")
                    .foregroundStyle(.secondary)
                Text(model.selectedModel?.name ?? "Choose a model")
                    .lineLimit(1)
                if model.switchingModel {
                    ProgressView()
                        .controlSize(.mini)
                }
            }
            .font(Console.mono(12))
        }
        .menuStyle(.button)
        .buttonStyle(.borderless)
        .fixedSize()
        .help(model.selectedModel?.note ?? "Choose the model that answers")
        .accessibilityIdentifier("modelMenu")
    }

    private var providerMenus: some View {
        ForEach(model.routerProviders, id: \.0) { provider, rows in
            Menu(provider) {
                ForEach(rows) { row in
                    Toggle(isOn: routerBinding(for: row)) {
                        Text(row.name)
                        Text(model.orcaConnected ? row.priceLabel : "\(row.priceLabel) · connect first")
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var orcaActions: some View {
        if !model.orcaConnected {
            Button("Connect OrcaRouter…") { model.showConnect = true }
        }
        Button("All OrcaRouter Models…") { model.showBrowser = true }
    }

    private func itemSubtitle(_ item: CatalogModel) -> String {
        if item.provider == "local" && !item.available { return "Not installed on this Mac" }
        if item.provider == "orcarouter" && !model.orcaConnected { return "\(item.subtitle) · connect first" }
        return item.subtitle
    }

    private func routerBinding(for row: RouterModel) -> Binding<Bool> {
        Binding(
            get: { model.selectedModelId == "orca:\(row.id)" },
            set: { isOn in
                if isOn { model.chooseRouterModel(row.id) }
            }
        )
    }

    private func binding(for item: CatalogModel) -> Binding<Bool> {
        Binding(
            get: { model.selectedModelId == item.id },
            set: { isOn in
                if isOn { model.selectModel(item.id) }
            }
        )
    }
}

/// Reasoning on or off, for models that can switch it (Qwen, Nemotron).
struct ReasoningMenu: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Menu {
            Toggle(isOn: Binding(get: { model.reasoningOn }, set: { if $0 { model.setReasoning(true) } })) {
                Text("Reasoning On")
                Text("Thinks before answering: better on hard tasks")
            }
            Toggle(isOn: Binding(get: { !model.reasoningOn }, set: { if $0 { model.setReasoning(false) } })) {
                Text("Reasoning Off")
                Text("Answers right away: faster and cheaper")
            }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "brain")
                Text(model.reasoningOn ? "Reasoning" : "Reasoning off")
            }
            .font(.callout)
            .foregroundStyle(model.reasoningOn ? AnyShapeStyle(.secondary) : AnyShapeStyle(.tertiary))
        }
        .menuStyle(.button)
        .buttonStyle(.borderless)
        .fixedSize()
        .disabled(model.running)
        .help(model.reasoningOn ? "The model thinks before it answers. Click to turn it off."
              : "The model answers without thinking first. Click to turn it on.")
        .accessibilityIdentifier("reasoningMenu")
    }
}
