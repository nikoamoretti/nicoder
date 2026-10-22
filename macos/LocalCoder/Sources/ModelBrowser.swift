import SwiftUI

/// Every OrcaRouter model that can use Local Coder's tools: search, sort, pick.
struct ModelBrowser: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var order: Order = .smartest
    @State private var freeOnly = false
    @State private var selection: RouterModel.ID?

    enum Order: String, CaseIterable, Identifiable {
        case smartest = "Smartest"
        case cheapest = "Cheapest"
        case fastest = "Fastest"
        var id: String { rawValue }
    }

    private var rows: [RouterModel] {
        let words = query.lowercased().split(separator: " ").map(String.init)
        let matching = model.routerModels.filter { row in
            (!freeOnly || row.free)
                && words.allSatisfy { word in
                    "\(row.name) \(row.provider) \(row.id)".lowercased().contains(word)
                }
        }
        switch order {
        case .smartest:
            return matching.sorted { ($0.intelligence ?? -1, $1.name) > ($1.intelligence ?? -1, $0.name) }
        case .cheapest:
            return matching.sorted { Self.price($0) < Self.price($1) }
        case .fastest:
            return matching.sorted { ($0.speed ?? -1, $1.name) > ($1.speed ?? -1, $0.name) }
        }
    }

    private static func price(_ row: RouterModel) -> Double {
        if row.free { return -1 }
        guard let priceIn = row.priceIn, let priceOut = row.priceOut else { return .greatestFiniteMagnitude }
        // An agent run is mostly reading: weigh input three to one.
        return priceIn * 3 + priceOut
    }

    private var selectedRow: RouterModel? {
        model.routerModels.first(where: { $0.id == selection })
    }

    private var currentID: String? {
        model.selectedModel?.provider == "orcarouter" ? String(model.selectedModelId.dropFirst("orca:".count)) : nil
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            if !model.orcaConnected {
                connectStrip
            }
            table
            footer
        }
        .frame(width: 860, height: 620)
        .onAppear {
            selection = currentID
            if model.routerModels.isEmpty { model.loadRouterModels() }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("OrcaRouter Models")
                        .font(.title2.weight(.semibold))
                    Text(subtitle)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    model.loadRouterModels(refresh: true)
                } label: {
                    if model.routerLoading {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Refresh", systemImage: "arrow.clockwise")
                    }
                }
                .disabled(model.routerLoading)
                .help("Download the model list again")
            }
            HStack(spacing: 12) {
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(.secondary)
                    TextField("Search by name or provider", text: $query)
                        .textFieldStyle(.plain)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(.background.secondary, in: .rect(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.separator.opacity(0.6)))
                Picker("Sort", selection: $order) {
                    ForEach(Order.allCases) { order in
                        Text(order.rawValue).tag(order)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                Toggle("Free only", isOn: $freeOnly)
                    .toggleStyle(.checkbox)
            }
        }
        .padding(.horizontal, 22)
        .padding(.top, 20)
        .padding(.bottom, 14)
    }

    private var subtitle: String {
        if let error = model.routerError, model.routerModels.isEmpty { return error }
        var text = "\(model.routerModels.count) models that can use Local Coder's tools"
        if let date = model.routerFetchedAt {
            text += " · updated \(date.formatted(.relative(presentation: .named)))"
        }
        return text
    }

    private var connectStrip: some View {
        HStack(spacing: 10) {
            Image(systemName: "key")
                .foregroundStyle(.tint)
            Text("Connect OrcaRouter to use these models. Browsing is free.")
                .font(.callout)
            Spacer()
            Button("Connect…") { model.showConnect = true }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 9)
        .background(.tint.opacity(0.08))
    }

    private var table: some View {
        Table(rows, selection: $selection) {
            TableColumn("Model") { row in
                HStack(spacing: 6) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(row.name)
                            .fontWeight(.medium)
                            .lineLimit(1)
                        Text(row.provider)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    if row.id == currentID {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(.tint)
                            .help("In use")
                    }
                }
            }
            .width(min: 200, ideal: 270)
            TableColumn("Intelligence") { row in
                ScoreBar(score: row.intelligence)
            }
            .width(min: 100, ideal: 120)
            TableColumn("Speed") { row in
                Text(row.speed.map { "\($0) tok/s" } ?? "–")
                    .monospacedDigit()
                    .foregroundStyle(row.speed == nil ? .tertiary : .primary)
            }
            .width(min: 64, ideal: 78)
            TableColumn("Price per million tokens") { row in
                Text(row.priceLabel)
                    .monospacedDigit()
                    .foregroundStyle(row.free ? AnyShapeStyle(.green) : AnyShapeStyle(.primary))
            }
            .width(min: 130, ideal: 170)
            TableColumn("Context") { row in
                Text(row.contextLabel)
                    .monospacedDigit()
            }
            .width(min: 52, ideal: 60)
        }
        .contextMenu(forSelectionType: RouterModel.ID.self) { _ in
        } primaryAction: { ids in
            if let id = ids.first { use(id) }
        }
        .overlay {
            if rows.isEmpty && !model.routerLoading {
                ContentUnavailableView.search(text: query)
            }
        }
    }

    private var footer: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                if let row = selectedRow {
                    HStack(spacing: 6) {
                        Text(row.name).font(.headline)
                        if row.reasoning { Tag(text: "Reasons") }
                        if row.vision { Tag(text: "Sees images") }
                        if row.free { Tag(text: "Free") }
                    }
                    Text(row.summary.isEmpty ? row.id : row.summary)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                } else {
                    Text("Intelligence is the Artificial Analysis index from OrcaRouter's public board.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 12)
            Button("Cancel") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("Use Model") {
                if let selection { use(selection) }
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
            .disabled(selection == nil)
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
        .background(.bar)
    }

    private func use(_ id: String) {
        dismiss()
        model.chooseRouterModel(id)
    }
}

private struct Tag: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(.quaternary.opacity(0.6), in: .capsule)
    }
}

/// A score with a short bar, scaled so the top models fill it.
struct ScoreBar: View {
    let score: Double?

    var body: some View {
        if let score {
            HStack(spacing: 8) {
                Text(String(format: "%.0f", score))
                    .monospacedDigit()
                    .frame(width: 22, alignment: .trailing)
                Capsule()
                    .fill(.quaternary)
                    .frame(width: 64, height: 5)
                    .overlay(alignment: .leading) {
                        Capsule()
                            .fill(.tint)
                            .frame(width: max(3, 64 * min(score, 70) / 70), height: 5)
                    }
            }
        } else {
            Text("–")
                .foregroundStyle(.tertiary)
        }
    }
}

/// Connect an OrcaRouter account: one click in the browser, or a pasted key.
struct ConnectOrcaRouterView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var key = ""
    @State private var saving = false

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "point.3.filled.connected.trianglepath.dotted")
                .font(.system(size: 36, weight: .light))
                .foregroundStyle(.tint)
                .padding(.top, 6)
            VStack(spacing: 6) {
                Text("Connect OrcaRouter")
                    .font(.title2.weight(.semibold))
                Text("One account for \(model.routerModels.isEmpty ? "150+" : String(model.routerModels.count)) models from OpenAI, Anthropic, Google, DeepSeek, Qwen and more. You pay each provider's own price, and Local Coder shows what every request cost.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 400)
            }
            if model.signingIn {
                VStack(spacing: 10) {
                    ProgressView()
                    Text(model.signInStatus ?? "Waiting for the browser…")
                        .font(.callout)
                        .multilineTextAlignment(.center)
                    Button("Cancel Sign-In") { model.cancelSignIn() }
                }
                .padding(.vertical, 8)
            } else {
                Button {
                    model.signInWithOrcaRouter()
                } label: {
                    Text("Sign In with OrcaRouter")
                        .frame(minWidth: 240)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                HStack {
                    VStack { Divider() }
                    Text("or paste a key")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize()
                    VStack { Divider() }
                }
                .frame(maxWidth: 380)
                HStack(spacing: 8) {
                    SecureField("sk-orca-…", text: $key)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit(save)
                    Button("Save", action: save)
                        .disabled(key.trimmingCharacters(in: .whitespaces).isEmpty || saving)
                }
                .frame(maxWidth: 380)
                Link("Get a key at orcarouter.ai/console", destination: URL(string: "https://www.orcarouter.ai/console")!)
                    .font(.callout)
            }
            if let error = model.connectError {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 400)
            }
            HStack {
                Label("The key stays in this Mac's Keychain.", systemImage: "lock")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Close") {
                    if model.signingIn { model.cancelSignIn() }
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
            }
            .padding(.top, 4)
        }
        .padding(28)
        .frame(width: 500)
    }

    private func save() {
        let value = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, !saving else { return }
        saving = true
        Task {
            if await model.saveOrcaKey(value) {
                key = ""
            }
            saving = false
        }
    }
}
