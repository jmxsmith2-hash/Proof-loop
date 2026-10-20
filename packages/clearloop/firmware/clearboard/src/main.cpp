// ClearBoard: open-source ejector controller for ClearLoop.
//
// Drives a plate-bending linear actuator and an optional door opener through a
// dual H-bridge, measures motor current to tell a finished bend from a stall,
// and can be triggered three independent ways:
//   1. HTTP from the ClearLoop controller (POST /api/cycle) - primary.
//   2. Stand-alone: watches the printer's own LAN MQTT for ClearLoop trigger
//      markers (M73 L<base+code>), no controller needed.
//   3. A limit switch the bed presses at the bend position - backup.
// A lockout stops two paths from bending the same plate twice, and a print that
// finishes without any trigger is flagged instead of passing silently.
//
// License: GPL-3.0-or-later.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <WebServer.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include "config.h"

// ---------------------------------------------------------------------------
// Settings (persisted in NVS)
// ---------------------------------------------------------------------------
struct Settings {
  String ssid, pass, token;
  String printerIp, printerSerial, accessCode;
  bool standalone = false;  // watch the printer's MQTT for markers
  bool doorFitted = false;
  int fullTravelMs = DEF_FULL_TRAVEL_MS;
  float stallAmps = DEF_STALL_AMPS;
  int stallMs = DEF_STALL_MS;
  float endAmps = DEF_END_AMPS;
  float ampsPerVolt = DEF_AMPS_PER_VOLT;
  int doorTravelMs = DEF_DOOR_TRAVEL_MS;
  float doorStallAmps = DEF_DOOR_STALL_AMPS;
  int markerBase = DEF_MARKER_BASE;
  uint32_t lockoutMs = DEF_CYCLE_LOCKOUT_MS;
  int fanSeconds = DEF_FAN_SECONDS;
};

Preferences prefs;
Settings S;
WebServer http(80);
WiFiClientSecure tls;
PubSubClient mqtt(tls);

void loadSettings() {
  prefs.begin("clearboard", true);
  S.ssid = prefs.getString("ssid", "");
  S.pass = prefs.getString("pass", "");
  S.token = prefs.getString("token", "");
  S.printerIp = prefs.getString("pip", "");
  S.printerSerial = prefs.getString("pser", "");
  S.accessCode = prefs.getString("pcode", "");
  S.standalone = prefs.getBool("standalone", false);
  S.doorFitted = prefs.getBool("door", false);
  S.fullTravelMs = prefs.getInt("travel", DEF_FULL_TRAVEL_MS);
  S.stallAmps = prefs.getFloat("stallA", DEF_STALL_AMPS);
  S.stallMs = prefs.getInt("stallMs", DEF_STALL_MS);
  S.endAmps = prefs.getFloat("endA", DEF_END_AMPS);
  S.ampsPerVolt = prefs.getFloat("apv", DEF_AMPS_PER_VOLT);
  S.doorTravelMs = prefs.getInt("dtravel", DEF_DOOR_TRAVEL_MS);
  S.doorStallAmps = prefs.getFloat("dstallA", DEF_DOOR_STALL_AMPS);
  S.markerBase = prefs.getInt("mbase", DEF_MARKER_BASE);
  S.lockoutMs = prefs.getUInt("lockout", DEF_CYCLE_LOCKOUT_MS);
  S.fanSeconds = prefs.getInt("fan", DEF_FAN_SECONDS);
  prefs.end();
}

// ---------------------------------------------------------------------------
// Counters, status
// ---------------------------------------------------------------------------
struct Counters {
  uint32_t cycles = 0, stalls = 0, doorCycles = 0, missedTriggers = 0;
} C;

struct LastResult {
  bool ok = true;
  bool stalled = false;
  String message = "no cycle yet";
  float durationS = 0;
  float peakAmps = 0;
  String profile = "";
  String source = "";
} last;

volatile bool estop = false;
bool armed = false;            // JOB_START seen for the current print
bool ejectedThisLoop = false;  // a bend already ran for the current print
bool triggerMissed = false;
uint32_t lastCycleAt = 0;
bool apMode = false;
uint32_t fanOffAt = 0;

void saveCounters() {
  prefs.begin("clearboard", false);
  prefs.putUInt("cycles", C.cycles);
  prefs.putUInt("stalls", C.stalls);
  prefs.putUInt("doors", C.doorCycles);
  prefs.putUInt("missed", C.missedTriggers);
  prefs.end();
}

void loadCounters() {
  prefs.begin("clearboard", true);
  C.cycles = prefs.getUInt("cycles", 0);
  C.stalls = prefs.getUInt("stalls", 0);
  C.doorCycles = prefs.getUInt("doors", 0);
  C.missedTriggers = prefs.getUInt("missed", 0);
  prefs.end();
}

// ---------------------------------------------------------------------------
// Motor channels with current sensing
// ---------------------------------------------------------------------------
struct Channel {
  const char* name;
  uint8_t in1, in2, sense;
  uint8_t ledc1, ledc2;
};

Channel BENDER = {"bender", PIN_BENDER_IN1, PIN_BENDER_IN2, PIN_BENDER_ISENSE, 0, 1};
Channel DOOR = {"door", PIN_DOOR_IN1, PIN_DOOR_IN2, PIN_DOOR_ISENSE, 2, 3};

const int PWM_FREQ = 20000;  // above hearing range: no PWM whine
const int PWM_BITS = 10;
const int PWM_MAX = (1 << PWM_BITS) - 1;

void setupChannel(Channel& ch) {
  ledcSetup(ch.ledc1, PWM_FREQ, PWM_BITS);
  ledcSetup(ch.ledc2, PWM_FREQ, PWM_BITS);
  ledcAttachPin(ch.in1, ch.ledc1);
  ledcAttachPin(ch.in2, ch.ledc2);
  ledcWrite(ch.ledc1, 0);
  ledcWrite(ch.ledc2, 0);
  analogSetPinAttenuation(ch.sense, ADC_11db);
}

void drive(Channel& ch, int dir, int duty) {
  // dir > 0 extends, dir < 0 retracts, 0 coasts.
  ledcWrite(ch.ledc1, dir > 0 ? duty : 0);
  ledcWrite(ch.ledc2, dir < 0 ? duty : 0);
}

float amps(Channel& ch) {
  uint32_t mv = 0;
  for (int i = 0; i < 4; i++) mv += analogReadMilliVolts(ch.sense);
  return (mv / 4.0f / 1000.0f) * S.ampsPerVolt;
}

enum class Stop { DONE, END, STALL, TIMEOUT, ESTOP };

struct Move {
  Stop stop;
  float peak;
  uint32_t ms;
};

// Run one channel. untilEnd: run until the actuator's internal end switch cuts
// the current (a full stroke); otherwise run for maxMs (a partial stroke).
Move move(Channel& ch, int dir, uint32_t maxMs, float dutyFrac, uint32_t rampMs, bool untilEnd, float stallAmps) {
  const uint32_t t0 = millis();
  uint32_t overSince = 0;
  float peak = 0;
  const int target = (int)(PWM_MAX * constrain(dutyFrac, 0.2f, 1.0f));
  while (true) {
    const uint32_t t = millis() - t0;
    if (estop) {
      drive(ch, 0, 0);
      return {Stop::ESTOP, peak, t};
    }
    const int duty = rampMs > 0 && t < rampMs ? (int)(target * (0.25f + 0.75f * t / (float)rampMs)) : target;
    drive(ch, dir, duty);
    delay(5);
    const float a = amps(ch);
    if (t > 120) peak = max(peak, a);  // ignore inrush
    if (t > 120 && a > stallAmps) {
      if (!overSince) overSince = millis();
      if (millis() - overSince >= (uint32_t)S.stallMs) {
        drive(ch, 0, 0);
        return {Stop::STALL, peak, t};
      }
    } else {
      overSince = 0;
    }
    if (untilEnd && t > rampMs + 300 && a < S.endAmps) {
      drive(ch, 0, 0);
      return {Stop::END, peak, t};
    }
    if (t >= maxMs) {
      drive(ch, 0, 0);
      return {untilEnd ? Stop::TIMEOUT : Stop::DONE, peak, t};
    }
  }
}

// ---------------------------------------------------------------------------
// Bend profiles
// ---------------------------------------------------------------------------
struct Profile {
  const char* name;
  float duty;        // 1.0 = full speed
  uint32_t rampMs;   // soft start
  float firstBend;   // fraction of travel for the first, partial bend
  float relax;       // fraction to back off before the full flex
  uint32_t holdMs;   // pause at full flex
};

const Profile STANDARD = {"standard", 1.0f, 150, 0.55f, 0.25f, 800};
const Profile QUIET = {"quiet", 0.6f, 900, 0.55f, 0.25f, 1200};

const Profile& profileByName(const String& n) { return n == "quiet" ? QUIET : STANDARD; }

uint32_t strokeMs(float frac, const Profile& p) { return (uint32_t)(frac * S.fullTravelMs / p.duty); }

void retractBender(Move& m) {
  m = move(BENDER, -1, strokeMs(1.3f, STANDARD), 0.8f, 200, true, S.stallAmps);
}

bool runCycle(const Profile& p, const String& source) {
  const uint32_t t0 = millis();
  float peak = 0;
  last = LastResult();
  last.profile = p.name;
  last.source = source;
  auto fail = [&](const char* why, bool stalled) {
    Move back;
    retractBender(back);
    last.ok = false;
    last.stalled = stalled;
    last.message = why;
    last.peakAmps = peak;
    last.durationS = (millis() - t0) / 1000.0f;
    if (stalled) C.stalls++;
    saveCounters();
    return false;
  };
  if (estop) return fail("emergency stop is latched", false);

  // Two-phase bend: partial flex, relax, then full flex. Releasing in stages
  // peels parts off gradually instead of shocking the plate.
  Move m = move(BENDER, +1, strokeMs(p.firstBend, p), p.duty, p.rampMs, false, S.stallAmps);
  peak = max(peak, m.peak);
  if (m.stop == Stop::STALL) return fail("stalled during the first bend (plate jammed or misaligned?)", true);
  if (m.stop == Stop::ESTOP) return fail("emergency stop", false);
  delay(300);
  m = move(BENDER, -1, strokeMs(p.relax, p), p.duty, p.rampMs / 2, false, S.stallAmps);
  peak = max(peak, m.peak);
  if (m.stop == Stop::ESTOP) return fail("emergency stop", false);
  m = move(BENDER, +1, strokeMs(1.3f, p), p.duty, p.rampMs, true, S.stallAmps);
  peak = max(peak, m.peak);
  if (m.stop == Stop::STALL) return fail("stalled before full flex", true);
  if (m.stop == Stop::TIMEOUT) return fail("never reached full flex (check actuator wiring or end switch)", false);
  if (m.stop == Stop::ESTOP) return fail("emergency stop", false);
  delay(p.holdMs);
  retractBender(m);
  peak = max(peak, m.peak);
  if (m.stop != Stop::END) return fail("did not retract fully; plate may still be bent", m.stop == Stop::STALL);

  C.cycles++;
  saveCounters();
  last.ok = true;
  last.message = "bend complete";
  last.peakAmps = peak;
  last.durationS = (millis() - t0) / 1000.0f;
  lastCycleAt = millis();
  ejectedThisLoop = true;
  return true;
}

bool door(bool open, String& why) {
  if (!S.doorFitted) {
    why = "no door opener fitted";
    return true;
  }
  if (estop) {
    why = "emergency stop is latched";
    return false;
  }
  Move m = move(DOOR, open ? +1 : -1, (uint32_t)(S.doorTravelMs * 1.4f), 0.8f, 300, true, S.doorStallAmps);
  if (m.stop == Stop::STALL) {
    move(DOOR, open ? -1 : +1, 500, 0.6f, 0, false, S.doorStallAmps);  // back off the obstacle
    why = "obstacle detected; door backed off";
    return false;
  }
  if (m.stop != Stop::END) {
    why = "door did not reach its end position";
    return false;
  }
  C.doorCycles++;
  why = open ? "door open" : "door closed";
  return true;
}

bool retractAll(String& why) {
  Move m;
  retractBender(m);
  String d;
  const bool doorOk = door(false, d);
  why = (m.stop == Stop::END ? String("bender retracted") : String("bender did not confirm retraction")) + "; " + d;
  return m.stop == Stop::END && doorOk;
}

// ---------------------------------------------------------------------------
// Trigger arbitration
// ---------------------------------------------------------------------------
bool lockedOut() { return lastCycleAt && millis() - lastCycleAt < S.lockoutMs; }

enum class Pending { NONE, JOB_START, DOOR_OPEN, EJECT };
Pending pending = Pending::NONE;

void onMarker(int code) {
  switch (code) {
    case 1: pending = Pending::JOB_START; break;
    case 2: pending = Pending::DOOR_OPEN; break;
    case 3: pending = Pending::EJECT; break;
    default: break;
  }
}

// ---------------------------------------------------------------------------
// Stand-alone mode: the printer's LAN MQTT
// ---------------------------------------------------------------------------
long lastLayer = -1;
String lastGcodeState = "";

void onMqtt(char* /*topic*/, byte* payload, unsigned int len) {
  JsonDocument filter;
  filter["print"]["layer_num"] = true;
  filter["print"]["gcode_state"] = true;
  JsonDocument doc;
  if (deserializeJson(doc, payload, len, DeserializationOption::Filter(filter))) return;
  JsonVariant p = doc["print"];
  if (p.isNull()) return;
  if (!p["layer_num"].isNull()) {
    long layer = p["layer_num"].as<long>();
    if (layer != lastLayer) {
      lastLayer = layer;
      onMarker((int)(layer - S.markerBase));
    }
  }
  if (!p["gcode_state"].isNull()) {
    String gs = p["gcode_state"].as<String>();
    if (gs != lastGcodeState) {
      lastGcodeState = gs;
      if (gs == "FINISH" && armed && !ejectedThisLoop) {
        triggerMissed = true;  // never silent: LED error pattern + status flag
        C.missedTriggers++;
        saveCounters();
      }
      if (gs == "FINISH" || gs == "FAILED") armed = false;
    }
  }
}

uint32_t lastMqttAttempt = 0;

void mqttService() {
  if (!S.standalone || apMode || S.printerIp.isEmpty()) return;
  if (mqtt.connected()) {
    mqtt.loop();
    return;
  }
  if (millis() - lastMqttAttempt < 5000) return;
  lastMqttAttempt = millis();
  tls.setInsecure();  // printers use a self-signed certificate; the access code authenticates
  mqtt.setServer(S.printerIp.c_str(), 8883);
  mqtt.setBufferSize(24 * 1024);
  mqtt.setKeepAlive(60);
  mqtt.setCallback(onMqtt);
  String id = "clearboard-" + String((uint32_t)ESP.getEfuseMac(), HEX);
  if (mqtt.connect(id.c_str(), "bblp", S.accessCode.c_str())) {
    mqtt.subscribe(("device/" + S.printerSerial + "/report").c_str());
  }
}

// ---------------------------------------------------------------------------
// HTTP API
// ---------------------------------------------------------------------------
bool authorized() {
  if (S.token.isEmpty()) return true;
  return http.header("Authorization") == "Bearer " + S.token;
}

void sendJson(int code, JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  http.send(code, "application/json", out);
}

void resultJson(JsonDocument& doc) {
  doc["ok"] = last.ok;
  doc["stalled"] = last.stalled;
  doc["message"] = last.message;
  doc["duration_s"] = last.durationS;
  doc["peak_current_a"] = last.peakAmps;
  doc["profile"] = last.profile;
  doc["source"] = last.source;
  doc["cycles_total"] = C.cycles;
}

JsonDocument body() {
  JsonDocument doc;
  if (http.hasArg("plain")) deserializeJson(doc, http.arg("plain"));
  return doc;
}

void handleStatus() {
  JsonDocument doc;
  doc["fw"] = FW_VERSION;
  doc["uptime_s"] = millis() / 1000;
  doc["ip"] = apMode ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
  doc["rssi"] = WiFi.RSSI();
  doc["ap_mode"] = apMode;
  doc["standalone"] = S.standalone;
  doc["mqtt_connected"] = mqtt.connected();
  doc["armed"] = armed;
  doc["ejected_this_loop"] = ejectedThisLoop;
  doc["trigger_missed"] = triggerMissed;
  doc["estop"] = (bool)estop;
  doc["locked_out"] = lockedOut();
  doc["bender_amps"] = amps(BENDER);
  doc["door_fitted"] = S.doorFitted;
  JsonObject c = doc["counters"].to<JsonObject>();
  c["cycles"] = C.cycles;
  c["stalls"] = C.stalls;
  c["door_cycles"] = C.doorCycles;
  c["missed_triggers"] = C.missedTriggers;
  JsonDocument r;
  resultJson(r);
  doc["last"] = r;
  sendJson(200, doc);
}

void handleCycle() {
  if (!authorized()) return http.send(401, "application/json", "{\"ok\":false,\"message\":\"bad token\"}");
  JsonDocument in = body();
  JsonDocument out;
  if (lockedOut() && !in["force"].as<bool>()) {
    // Another trigger path already bent this plate; report the earlier result.
    resultJson(out);
    out["message"] = "already bent for this loop (lockout); returning the previous result";
    return sendJson(200, out);
  }
  runCycle(profileByName(in["profile"] | "standard"), "http");
  resultJson(out);
  sendJson(last.ok ? 200 : 500, out);
}

void handleRetract() {
  if (!authorized()) return http.send(401, "application/json", "{\"ok\":false}");
  String why;
  const bool ok = retractAll(why);
  armed = true;
  ejectedThisLoop = false;
  triggerMissed = false;
  JsonDocument out;
  out["ok"] = ok;
  out["message"] = why;
  sendJson(200, out);
}

void handleDoor() {
  if (!authorized()) return http.send(401, "application/json", "{\"ok\":false}");
  JsonDocument in = body();
  String why;
  bool ok = door(in["open"] | true, why);
  JsonDocument out;
  out["ok"] = ok;
  out["message"] = why;
  sendJson(ok ? 200 : 500, out);
}

void handleEstopReset() {
  if (!authorized()) return http.send(401, "application/json", "{\"ok\":false}");
  if (digitalRead(PIN_ESTOP) == HIGH) return http.send(409, "application/json", "{\"ok\":false,\"message\":\"button still pressed\"}");
  estop = false;
  http.send(200, "application/json", "{\"ok\":true}");
}

void handleConfig() {
  if (!authorized()) return http.send(401, "application/json", "{\"ok\":false}");
  JsonDocument in = body();
  prefs.begin("clearboard", false);
  bool restart = false;
  auto str = [&](const char* key, const char* nvs, bool needsRestart) {
    if (!in[key].isNull()) {
      prefs.putString(nvs, in[key].as<String>());
      restart |= needsRestart;
    }
  };
  str("ssid", "ssid", true);
  str("password", "pass", true);
  str("token", "token", false);
  str("printer_ip", "pip", true);
  str("printer_serial", "pser", true);
  str("access_code", "pcode", true);
  if (!in["standalone"].isNull()) { prefs.putBool("standalone", in["standalone"]); restart = true; }
  if (!in["door_fitted"].isNull()) prefs.putBool("door", in["door_fitted"]);
  if (!in["full_travel_ms"].isNull()) prefs.putInt("travel", in["full_travel_ms"]);
  if (!in["stall_amps"].isNull()) prefs.putFloat("stallA", in["stall_amps"]);
  if (!in["stall_ms"].isNull()) prefs.putInt("stallMs", in["stall_ms"]);
  if (!in["end_amps"].isNull()) prefs.putFloat("endA", in["end_amps"]);
  if (!in["amps_per_volt"].isNull()) prefs.putFloat("apv", in["amps_per_volt"]);
  if (!in["door_travel_ms"].isNull()) prefs.putInt("dtravel", in["door_travel_ms"]);
  if (!in["door_stall_amps"].isNull()) prefs.putFloat("dstallA", in["door_stall_amps"]);
  if (!in["marker_base"].isNull()) prefs.putInt("mbase", in["marker_base"]);
  if (!in["lockout_ms"].isNull()) prefs.putUInt("lockout", in["lockout_ms"]);
  if (!in["fan_seconds"].isNull()) prefs.putInt("fan", in["fan_seconds"]);
  prefs.end();
  loadSettings();
  JsonDocument out;
  out["ok"] = true;
  out["restart_required"] = restart;
  sendJson(200, out);
}

const char INDEX_HTML[] PROGMEM = R"HTML(<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>ClearBoard</title><style>body{font:15px system-ui;margin:16px;max-width:640px}button{margin:4px;padding:8px 12px}pre{background:#eee;padding:8px;overflow:auto}</style></head>
<body><h2>ClearBoard</h2><div>
<button onclick="go('/api/cycle',{profile:'standard'})">Test bend</button>
<button onclick="go('/api/cycle',{profile:'quiet'})">Test quiet bend</button>
<button onclick="go('/api/retract',{})">Retract</button>
<button onclick="go('/api/door',{open:true})">Open door</button>
<button onclick="go('/api/door',{open:false})">Close door</button>
<button onclick="go('/api/estop/reset',{})">Reset e-stop</button></div>
<pre id=o>loading</pre><script>
const t=localStorage.getItem('cbtoken')||'';
async function go(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+t},body:JSON.stringify(Object.assign({force:true},b))});o.textContent=await r.text();}
async function s(){o.textContent=JSON.stringify(await (await fetch('/api/status')).json(),null,2)}s();setInterval(s,3000)
</script></body></html>)HTML";

void setupHttp() {
  const char* headers[] = {"Authorization"};
  http.collectHeaders(headers, 1);
  http.on("/", HTTP_GET, [] { http.send_P(200, "text/html", INDEX_HTML); });
  http.on("/api/status", HTTP_GET, handleStatus);
  http.on("/api/cycle", HTTP_POST, handleCycle);
  http.on("/api/retract", HTTP_POST, handleRetract);
  http.on("/api/door", HTTP_POST, handleDoor);
  http.on("/api/estop/reset", HTTP_POST, handleEstopReset);
  http.on("/api/config", HTTP_POST, handleConfig);
  http.on("/api/restart", HTTP_POST, [] {
    if (!authorized()) return http.send(401, "application/json", "{\"ok\":false}");
    http.send(200, "application/json", "{\"ok\":true}");
    delay(200);
    ESP.restart();
  });
  http.begin();
}

// ---------------------------------------------------------------------------
// Wi-Fi, LED, inputs
// ---------------------------------------------------------------------------
void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  if (!S.ssid.isEmpty()) {
    WiFi.begin(S.ssid.c_str(), S.pass.c_str());
    for (int i = 0; i < 80 && WiFi.status() != WL_CONNECTED; i++) delay(250);
  }
  if (WiFi.status() != WL_CONNECTED) {
    // First boot or wrong credentials: open a setup access point.
    apMode = true;
    String name = "ClearBoard-" + String((uint32_t)ESP.getEfuseMac() & 0xFFFF, HEX);
    WiFi.mode(WIFI_AP);
    WiFi.softAP(name.c_str(), "clearboard");
  }
}

void IRAM_ATTR onEstop() {
  // Only set the flag here (LEDC calls are not ISR-safe). Every motion loop polls
  // it every 5 ms and loop() cuts both bridges, so motors stop within a few ms.
  estop = true;
}

void ledService() {
  const uint32_t t = millis();
  bool on;
  if (estop || triggerMissed || !last.ok) {
    on = (t % 1500) < 600 && ((t % 1500) / 100) % 2 == 0;  // triple blink: needs attention
  } else if (apMode || WiFi.status() != WL_CONNECTED) {
    on = (t % 2000) < 1000;  // slow blink: no network
  } else if (S.standalone && !mqtt.connected()) {
    on = (t % 500) < 250;  // fast blink: printer MQTT not connected
  } else {
    on = true;  // solid: ready
  }
  digitalWrite(PIN_LED, on);
}

uint32_t switchLowSince = 0;

void switchService() {
  // Backup trigger: the bed presses this switch at the bend position.
  if (digitalRead(PIN_LIMIT_SW) == LOW) {
    if (!switchLowSince) switchLowSince = millis();
    if (millis() - switchLowSince > 40 && !lockedOut()) {
      runCycle(STANDARD, "switch");
      switchLowSince = 0;
    }
  } else {
    switchLowSince = 0;
  }
}

void pendingService() {
  Pending p = pending;
  pending = Pending::NONE;
  String why;
  switch (p) {
    case Pending::JOB_START:
      armed = true;
      ejectedThisLoop = false;
      triggerMissed = false;
      retractAll(why);
      break;
    case Pending::DOOR_OPEN:
      door(true, why);
      if (S.fanSeconds > 0) {
        digitalWrite(PIN_FAN, HIGH);
        fanOffAt = millis() + S.fanSeconds * 1000UL;
      }
      break;
    case Pending::EJECT:
      if (!lockedOut()) runCycle(STANDARD, "mqtt");
      break;
    case Pending::NONE:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_FAN, OUTPUT);
  digitalWrite(PIN_FAN, LOW);
  pinMode(PIN_LIMIT_SW, INPUT_PULLUP);
  pinMode(PIN_ESTOP, INPUT_PULLUP);
  loadSettings();
  loadCounters();
  setupChannel(BENDER);
  setupChannel(DOOR);
  // Normally-closed e-stop to GND: pressing it, or a broken wire, pulls the pin HIGH.
  attachInterrupt(digitalPinToInterrupt(PIN_ESTOP), onEstop, RISING);
  if (digitalRead(PIN_ESTOP) == HIGH) estop = true;

  // Always start from a known, retracted position so the printer can home and level.
  Move m;
  retractBender(m);

  setupWifi();
  setupHttp();
  Serial.printf("ClearBoard %s ready at %s\n", FW_VERSION,
                apMode ? WiFi.softAPIP().toString().c_str() : WiFi.localIP().toString().c_str());
}

void loop() {
  http.handleClient();
  mqttService();
  pendingService();
  switchService();
  if (fanOffAt && (int32_t)(millis() - fanOffAt) > 0) {
    digitalWrite(PIN_FAN, LOW);
    fanOffAt = 0;
  }
  static uint32_t lastReconnect = 0;
  if (!apMode && WiFi.status() != WL_CONNECTED && millis() - lastReconnect > 10000) {
    lastReconnect = millis();
    WiFi.reconnect();
  }
  if (estop) {
    drive(BENDER, 0, 0);
    drive(DOOR, 0, 0);
  }
  ledService();
  delay(2);
}
