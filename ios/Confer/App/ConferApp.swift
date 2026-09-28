import BackgroundTasks
import SwiftUI

@main
struct ConferApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .onChange(of: phase) { _, newPhase in
                    switch newPhase {
                    case .active: model.startPolling()
                    case .background: model.stopPolling(); scheduleRefresh()
                    default: break
                    }
                }
        }
        .backgroundTask(.appRefresh("io.github.ryancalpin.confer.refresh")) {
            await model.pollOnce()
            await MainActor.run { scheduleRefresh() }
        }
    }

    private func scheduleRefresh() {
        let req = BGAppRefreshTaskRequest(identifier: "io.github.ryancalpin.confer.refresh")
        req.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(req)
    }
}

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Group {
            if model.api == nil {
                OnboardingView()
            } else {
                MainTabs()
                    .task {
                        model.requestNotificationPermission()
                        await model.refreshAll()
                    }
            }
        }
        .overlay(alignment: .top) {
            if let msg = model.banner {
                Text(msg)
                    .font(.callout)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.red.opacity(0.9), in: RoundedRectangle(cornerRadius: 12))
                    .foregroundStyle(.white)
                    .padding(.horizontal)
                    .onTapGesture { model.banner = nil }
                    .task {
                        try? await Task.sleep(nanoseconds: 5_000_000_000)
                        model.banner = nil
                    }
            }
        }
    }
}

struct MainTabs: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        TabView {
            NavigationStack { InboxView() }
                .tabItem { Label("Inbox", systemImage: "tray") }
                .badge(model.actionableCount)
            NavigationStack { PlansView() }
                .tabItem { Label("Plans", systemImage: "calendar") }
            NavigationStack { TripsView() }
                .tabItem { Label("Trips", systemImage: "suitcase") }
            NavigationStack { ListsView() }
                .tabItem { Label("Lists", systemImage: "checklist") }
            NavigationStack { MoneyView() }
                .tabItem { Label("Money", systemImage: "dollarsign.circle") }
                .badge(model.moneyWaiting)
            NavigationStack { PeopleView() }
                .tabItem { Label("People", systemImage: "person.2") }
        }
    }
}
