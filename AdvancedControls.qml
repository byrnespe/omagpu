import QtQuick
import QtQuick.Controls
import qs.Commons

Column {
  id: advanced
  required property var host
  property string mode: "profiles"
  readonly property var state: host.automationState || ({})
  readonly property var config: state.config || ({})
  readonly property var caps: state.capabilities || ({})
  readonly property var profiles: state.profiles || ({})
  readonly property var profileNames: Object.keys(profiles)
  property bool dirtyCurve: false
  property bool dirtyRules: false
  spacing: Style.space(10)

  function numberOr(value, fallback) { return value === fallback ? fallback : Number(value) }
  function settings() {
    return {graphics: numberOr(graphics.currentText, "auto"), memory: numberOr(memory.currentText, "auto"),
      power: powerDefault.checked ? "default" : power.value, fan: fanAuto.checked ? "auto" : Math.round(fan.value * 255 / 100)}
  }
  function loadProfile() {
    var p = profiles[profilePicker.currentText]
    if (!p) return
    graphics.currentIndex = Math.max(0, graphics.model.indexOf(String(p.graphics)))
    memory.currentIndex = Math.max(0, memory.model.indexOf(String(p.memory)))
    powerDefault.checked = p.power === "default"
    power.value = p.power === "default" ? (caps.powerDefault || 0) : Number(p.power)
    fanAuto.checked = p.fan === "auto"
    fan.value = p.fan === "auto" ? 50 : Math.round(Number(p.fan) * 100 / 255)
    profileName.text = profilePicker.currentText
  }
  function submit(op, fields) { host.sendAutomation(Object.assign({op: op}, fields || ({}))) }

  component Caption: Text {
    width: parent.width
    textFormat: Text.PlainText
    color: Color.foreground
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
  }
  component Heading: Caption { font.pixelSize: Style.font.body; font.bold: true }

  Caption {
    visible: !host.currentGpu.supportsAutomation
    text: "Advanced controls need the controller update. Saved settings will not run until enabled."
  }
  Caption {
    visible: !!state.event
    text: state.event || ""
    color: state.latched ? host.urgent : Color.accent
  }
  Caption {
    text: "Controller: " + (state.status === "success" ? "ready" : "starting") + (state.temperature !== null && state.temperature !== undefined ? " · " + state.temperature + "°C" : "")
  }

  Column {
    width: parent.width
    spacing: Style.space(8)
    visible: advanced.mode === "profiles"
    enabled: Boolean(host.currentGpu.supportsAutomation) && !host.controlBusy && !state.latched
    Heading { text: "Saved profiles" }
    ComboBox { id: profilePicker; width: parent.width; model: advanced.profileNames; Accessible.name: "Saved GPU profile" }
    Flow {
      width: parent.width; spacing: Style.space(6)
      ControlButton { text: "Apply profile"; accent: true; enabled: profilePicker.currentIndex >= 0; onClicked: advanced.submit("apply-profile", {name: profilePicker.currentText}) }
      ControlButton { text: "Load into editor"; onClicked: advanced.loadProfile() }
      ControlButton { text: "Delete"; enabled: profilePicker.currentText !== "Default"; onClicked: advanced.submit("delete-profile", {name: profilePicker.currentText}) }
    }
    Caption { text: state.current ? "Restore point established · " + (state.activeProfile || "custom settings") : "Apply a saved profile to establish the restore point for app rules and timed overrides." }
    Heading { text: "GPU clock cap (MHz)" }
    Caption { text: "Caps core boost at the selected supported frequency; auto restores driver scaling." }
    ComboBox { id: graphics; width: parent.width; model: ["auto"].concat((advanced.caps.graphics || []).map(String)); Accessible.name: "GPU clock cap" }
    ControlButton { text: "Apply core cap"; onClicked: advanced.submit("manual", {key: "graphics", value: advanced.numberOr(graphics.currentText, "auto")}) }
    Heading { text: "Memory clock (MHz)" }
    ComboBox { id: memory; width: parent.width; model: ["auto"].concat((advanced.caps.memory || []).map(String)); Accessible.name: "Memory clock" }
    ControlButton { text: "Apply memory clock"; onClicked: advanced.submit("manual", {key: "memory", value: advanced.numberOr(memory.currentText, "auto")}) }
    Heading { text: "Profile power and cooling" }
    CheckBox { id: powerDefault; text: "Default power limit"; checked: true }
    SpinBox { id: power; from: Math.ceil(advanced.caps.powerMin || 0); to: Math.floor(advanced.caps.powerMax || 1); value: advanced.caps.powerDefault || 0; enabled: !powerDefault.checked; editable: true; Accessible.name: "Profile power limit in watts" }
    CheckBox { id: fanAuto; text: "Automatic fan control"; checked: true }
    SpinBox { id: fan; from: 0; to: 100; value: 50; enabled: !fanAuto.checked; editable: true; Accessible.name: "Profile fan percentage" }
    TextField { id: profileName; width: parent.width; placeholderText: "Profile name"; maximumLength: 48; Accessible.name: "Profile name" }
    ControlButton { text: "Save profile"; enabled: profileName.text.trim() !== ""; onClicked: advanced.submit("save-profile", {name: profileName.text, settings: advanced.settings()}) }
    Heading { text: "Timed override" }
    Caption { text: "Temporarily apply the selected saved profile, then restore the complete prior OmaGPU configuration. Works with the panel closed." }
    Row {
      spacing: Style.space(8)
      SpinBox { id: minutes; from: 1; to: 1440; value: 15; editable: true; Accessible.name: "Override minutes" }
      Text { text: "minutes"; color: Color.foreground; anchors.verticalCenter: parent.verticalCenter }
    }
    Flow {
      width: parent.width; spacing: Style.space(6)
      ControlButton { text: "Start timed profile"; enabled: !!state.current && !state.timerSeconds && !state.curve && !state.apps; onClicked: advanced.submit("temporary", {name: profilePicker.currentText, seconds: minutes.value * 60}) }
      ControlButton { text: "Restore now"; enabled: state.timerSeconds > 0; onClicked: advanced.submit("cancel-timer") }
    }
    Caption { text: state.timerSeconds > 0 ? "Restoring previous settings in " + state.timerSeconds + " seconds" : "No timed override active" }
  }

  Column {
    visible: advanced.mode === "automation"
    width: parent.width
    spacing: Style.space(8)
    enabled: Boolean(host.currentGpu.supportsAutomation) && !host.controlBusy
    Heading { text: "Custom fan curve" }
    Caption { text: "Temperature:fan percentage, one point per line. Points must increase and reach 100% fan by 85°C. Driver fan limits apply." }
    TextArea {
      id: curve
      width: parent.width
      implicitHeight: Style.space(115)
      text: "35:30\n50:40\n65:60\n75:80\n85:100"
      selectByMouse: true
      Accessible.name: "Fan curve temperature and duty points"
      onTextChanged: advanced.dirtyCurve = true
    }
    Flow {
      width: parent.width; spacing: Style.space(6)
      ControlButton {
        text: state.curve ? "Update curve" : "Enable curve"
        accent: Boolean(state.curve)
        enabled: !state.latched && !state.apps && !state.timerSeconds
        onClicked: {
          var points = curve.text.trim().split(/\n/).map(function(row) { return row.split(":").map(Number) })
          advanced.submit("set-curve", {enabled: true, points: points})
        }
      }
      ControlButton { text: "Stop / automatic fan"; enabled: Boolean(state.curve); onClicked: advanced.submit("set-curve", {enabled: false}) }
      ControlButton { text: "Load saved curve"; onClicked: curve.text = (advanced.config.curve || []).map(function(p) { return p.join(":") }).join("\n") }
    }
    Caption { text: state.curve ? "Curve active · watchdog restores automatic cooling if the controller stops" : "Curve stopped" }

    Heading { text: "Per-app profiles" }
    Caption { text: "Match an executable name or full path. The first matching rule wins. Previous settings return when the app exits. Only your own running processes are matched." }
    TextField { id: appName; width: parent.width; placeholderText: "Executable, e.g. blender"; Accessible.name: "Application executable" }
    ComboBox { id: appProfile; width: parent.width; model: advanced.profileNames; Accessible.name: "Application profile" }
    ControlButton {
      text: "Add app rule"
      enabled: appName.text.trim() !== ""
      onClicked: advanced.submit("save-rules", {rules: (advanced.config.rules || []).concat([{app: appName.text.trim(), profile: appProfile.currentText}])})
    }
    Repeater {
      model: advanced.config.rules || []
      delegate: Row {
        required property var modelData
        required property int index
        width: advanced.width
        spacing: Style.space(6)
        Caption { width: parent.width - removeRule.width - Style.space(6); text: modelData.app + " → " + modelData.profile }
        ControlButton { id: removeRule; text: "Remove"; onClicked: advanced.submit("save-rules", {rules: advanced.config.rules.filter(function(r, i) { return i !== index })}) }
      }
    }
    ControlButton { text: state.apps ? "Disable app switching" : "Enable app switching"; enabled: Boolean(state.apps) || (!!state.current && !state.curve && !state.timerSeconds && !state.latched); onClicked: advanced.submit("set-apps", {enabled: !state.apps}) }
    Caption { text: state.appActive ? "Matched: " + state.appActive : "No application override active" }

    Heading { text: "Temperature alerts" }
    SpinBox { id: alertTemp; from: 50; to: 95; value: 80; editable: true; Accessible.name: "Alert threshold Celsius" }
    Flow {
      width: parent.width; spacing: Style.space(6)
      ControlButton { text: state.alerts ? "Update alert" : "Enable alert"; onClicked: advanced.submit("set-alert", {temperature: alertTemp.value, enabled: true}) }
      ControlButton { text: "Disable alert"; enabled: Boolean(state.alerts); onClicked: advanced.submit("set-alert", {temperature: alertTemp.value, enabled: false}) }
    }
    Caption { text: state.alerts ? "Alert at " + advanced.config.alertTemp + "°C · 5°C rearm gap, 60s cooldown" : "Temperature alerts disabled" }

    Heading { text: "Thermal fallback" }
    Caption { text: "At the threshold, restore automatic cooling and clocks, lower power to the driver minimum, and stop overrides. Acknowledge after cooling 5°C below the threshold to resume control." }
    SpinBox { id: guardTemp; from: 50; to: 95; value: 85; editable: true; Accessible.name: "Thermal fallback threshold Celsius" }
    Flow {
      width: parent.width; spacing: Style.space(6)
      ControlButton { text: state.guard ? "Update fallback" : "Enable fallback"; onClicked: advanced.submit("set-guard", {temperature: guardTemp.value, enabled: true}) }
      ControlButton { text: "Disable fallback"; enabled: Boolean(state.guard) && !state.latched; onClicked: advanced.submit("set-guard", {temperature: guardTemp.value, enabled: false}) }
      ControlButton { text: "Acknowledge cooled GPU"; visible: Boolean(state.latched); onClicked: advanced.submit("acknowledge") }
    }
    Caption { text: state.guard ? "Fallback armed at " + advanced.config.guardTemp + "°C" : "Thermal fallback disabled" }
    Caption { text: "Automation starts disabled each desktop session. Saved profiles, rules, curve points and thresholds persist." }
  }
}
