import XCTest

/// Drives the app against a local demo node (see docs in ios/README.md):
/// accepts a money request and a plan invitation by tapping, like a person would.
final class ConferUITests: XCTestCase {
    func launch(tab: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-ConferDemoURL", ProcessInfo.processInfo.environment["CONFER_URL"] ?? "http://127.0.0.1:3067",
                               "-ConferDemoToken", ProcessInfo.processInfo.environment["CONFER_TOKEN"] ?? "demo-token-for-simulator",
                               "-ConferTab", tab]
        app.launch()
        return app
    }

    func testAcceptMoneyRequestFromMoneyTab() throws {
        let app = launch(tab: "money")
        let accept = app.buttons["Accept"].firstMatch
        XCTAssertTrue(accept.waitForExistence(timeout: 20), "money request should be listed")
        accept.tap()
        XCTAssertTrue(app.staticTexts["accepted"].waitForExistence(timeout: 15), "history should show the accepted entry")
    }

    func testAcceptPlanInvitation() throws {
        let app = launch(tab: "plans")
        let row = app.staticTexts["Dinner at Luigi's"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 20))
        row.tap()
        let accept = app.buttons["Accept"].firstMatch
        XCTAssertTrue(accept.waitForExistence(timeout: 10))
        accept.tap()
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.staticTexts["confirmed"].waitForExistence(timeout: 20), "the plan should confirm once Alex accepts")
    }
}
