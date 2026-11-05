/*
 * Pressure_Diagnostic v2 — Nano / ATmega328P, 9600 baud, newline commands.
 * Independent diagnostic sketch: preserves the historical sketches unchanged.
 * SELECT chooses S1..S5 / P0..P4, exactly one branch; analogue input always A2.
 * Digital readback does NOT verify physical ink position or analogue health.
 * Structured pressure_v2 rows carry channel, selection token, ADC, V and zero.
 * The 5.0 V reference is nominal; neither 2.5 V nor a manual zero is calibration.
 */
#include <Arduino.h>
#include <Wire.h>
#include <ctype.h>
#include <string.h>

const uint8_t TCA_ADDR = 0x20;
const uint8_t REG_INPUT0 = 0x00;
const uint8_t REG_OUTPUT0 = 0x02;
const uint8_t REG_OUTPUT1 = 0x03;
const uint8_t REG_POLARITY0 = 0x04;
const uint8_t REG_POLARITY1 = 0x05;
const uint8_t REG_CONFIG0 = 0x06;
const uint8_t REG_CONFIG1 = 0x07;
uint8_t selectedChannel = 3;
uint8_t selectedMask = 0x04;
uint32_t selectionToken = 0;  // boot/IDE only; host must select with fresh nonzero token
const uint8_t CONTROL_MASK = 0x7f;  // actual levels of control bits 0..6
const uint8_t ADC_COUNT = 10;
const uint8_t ZERO_FRAMES = 40;
const unsigned long FRAME_MS = 100;
const unsigned long SETTLE_MS = 20;
const unsigned long CHECK_MS = 250;
const float NOMINAL_VREF = 5.0f;

struct RegisterSnapshot {
  uint8_t input0, input1, output0, output1, polarity0, polarity1, config0, config1;
};
RegisterSnapshot registersNow = {};
bool registersValid = false;
bool ready = false;
bool faultLatched = false;
uint8_t lastRegister = 0;
uint8_t lastWireCode = 0;
bool lastWireTimeout = false;
unsigned long lastFrameAt = 0;
unsigned long lastCheckAt = 0;


bool baselineValid = false;
bool zeroCollecting = false;
uint8_t zeroCount = 0;
float zeroSum = 0.0f;
float baselineVoltage = 0.0f;
uint32_t zeroId = 0;
uint32_t zeroSequence = 0;
unsigned long zeroStartAt = 0;
unsigned long baselineAt = 0;
char commandBuffer[48];
uint8_t commandLength = 0;
bool commandOverflow = false;
bool commandInvalid = false;

void printHexByte(uint8_t value) {
  if (value < 16) Serial.print('0');
  Serial.print(value, HEX);
}

// The installed AVR Wire library supports the timeout API. Timeout recovery
// resets the I2C peripheral only; it never declares the external chip healthy.
bool writeRegister(uint8_t reg, uint8_t value) {
  lastRegister = reg;
  Wire.clearWireTimeoutFlag();
  Wire.beginTransmission(TCA_ADDR);
  Wire.write(reg);
  Wire.write(value);
  lastWireCode = Wire.endTransmission();
  lastWireTimeout = Wire.getWireTimeoutFlag();
  return lastWireCode == 0 && !lastWireTimeout;
}

bool readRegister(uint8_t reg, uint8_t &value) {
  lastRegister = reg;
  Wire.clearWireTimeoutFlag();
  Wire.beginTransmission(TCA_ADDR);
  Wire.write(reg);
  lastWireCode = Wire.endTransmission(false);
  lastWireTimeout = Wire.getWireTimeoutFlag();
  if (lastWireCode != 0 || lastWireTimeout) return false;
  uint8_t received = Wire.requestFrom(TCA_ADDR, (uint8_t)1);
  lastWireTimeout = Wire.getWireTimeoutFlag();
  if (received != 1 || Wire.available() != 1 || lastWireTimeout) {
    lastWireCode = 0xff;  // local diagnostic: missing/incomplete readback
    while (Wire.available()) Wire.read();
    return false;
  }
  value = (uint8_t)Wire.read();
  return true;
}

bool readRegisters() {
  registersValid = false;
  RegisterSnapshot next = {};
  if (!readRegister(0x00, next.input0) || !readRegister(0x01, next.input1) ||
      !readRegister(0x02, next.output0) || !readRegister(0x03, next.output1) ||
      !readRegister(0x04, next.polarity0) || !readRegister(0x05, next.polarity1) ||
      !readRegister(0x06, next.config0) || !readRegister(0x07, next.config1)) return false;
  registersNow = next;
  registersValid = true;
  return true;
}

bool registersMatchMask(uint8_t mask) {
  return registersValid && registersNow.config0 == 0 && registersNow.config1 == 0 &&
         registersNow.output0 == mask && registersNow.output1 == 0 &&
         registersNow.polarity0 == 0 && registersNow.polarity1 == 0 &&
         (registersNow.input0 & CONTROL_MASK) == mask;
}

void printRegisters() {
  Serial.print(F(" valid=")); Serial.print(registersValid ? 1 : 0);
  if (!registersValid) return;
  Serial.print(F(" in0=0x")); printHexByte(registersNow.input0);
  Serial.print(F(" in1=0x")); printHexByte(registersNow.input1);
  Serial.print(F(" out0=0x")); printHexByte(registersNow.output0);
  Serial.print(F(" out1=0x")); printHexByte(registersNow.output1);
  Serial.print(F(" pol0=0x")); printHexByte(registersNow.polarity0);
  Serial.print(F(" pol1=0x")); printHexByte(registersNow.polarity1);
  Serial.print(F(" cfg0=0x")); printHexByte(registersNow.config0);
  Serial.print(F(" cfg1=0x")); printHexByte(registersNow.config1);
}

void clearZero(const __FlashStringHelper *reason) {
  baselineValid = false;
  zeroCollecting = false;
  zeroCount = 0;
  zeroSum = 0.0f;
  baselineVoltage = 0.0f;
  baselineAt = 0;
  zeroStartAt = 0;
  zeroId = 0;
  Serial.print(F("# ZERO_CLEAR id=")); Serial.print(zeroId);
  Serial.print(F(" t=")); Serial.print(millis());
  Serial.print(F(" reason=")); Serial.println(reason);
}

// Best effort only: BOTH clear writes are attempted even if the first fails.
// A failed clear cannot be described as an off/unselected hardware state.
bool trySafeClear() {
  bool port0 = writeRegister(REG_OUTPUT0, 0);
  bool port1 = writeRegister(REG_OUTPUT1, 0);
  return port0 && port1;
}

void latchFault(const __FlashStringHelper *reason) {
  if (faultLatched) return;
  ready = false;
  faultLatched = true;
  uint8_t failedRegister = lastRegister;
  uint8_t failedCode = lastWireCode;
  bool failedTimeout = lastWireTimeout;
  clearZero(F("fault"));
  bool cleared = trySafeClear();
  Serial.print(F("# ERROR t=")); Serial.print(millis());
  Serial.print(F(" reason=")); Serial.print(reason);
  Serial.print(F(" reg=0x")); printHexByte(failedRegister);
  Serial.print(F(" wire=")); Serial.print(failedCode);
  Serial.print(F(" timeout=")); Serial.print(failedTimeout ? 1 : 0);
  Serial.print(F(" safe_clear=")); Serial.print(cleared ? F("acknowledged") : F("failed_state_unknown"));
  printRegisters();
  Serial.println(F(" latched=1 numeric_stopped=1 recover=I"));
}

void printHelp() {
  Serial.println(F("# HELP newline commands: Z/zero=start40frame_zero C/clear=cancel_clear D/status=read_registers I/init=reinitialize H/help=help"));
  Serial.println(F("# HELP zero uses FUTURE valid frames; stay unloaded. V and ADC remain nominal, dV is relative voltage only."));
  Serial.println(F("# HELP SELECT S1..S5 token selects one pressure branch; token=1..4294967295. Z token/C token supported."));
  Serial.println(F("# HELP Digital readback does not prove physical ink position or analogue health. No motor commands."));
}

void initializeSelection(bool boot) {
  ready = false;
  selectedChannel = 3;
  selectedMask = 0x04;
  selectionToken = 0;
  faultLatched = false;  // only this explicit path can attempt recovery
  registersValid = false;
  clearZero(boot ? F("reset") : F("operator_init"));
  // Latches go low before any output configuration is enabled. If one clear
  // fails, do not proceed to enable or select outputs; latchFault tries both again.
  bool cleared0 = writeRegister(REG_OUTPUT0, 0);
  uint8_t firstCode = lastWireCode;
  bool firstTimeout = lastWireTimeout;
  bool cleared1 = writeRegister(REG_OUTPUT1, 0);
  if (!cleared0 || !cleared1) {
    if (!cleared0) {
      lastRegister = REG_OUTPUT0;
      lastWireCode = firstCode;
      lastWireTimeout = firstTimeout;
    }
    latchFault(F("initial_latch_clear_failed"));
    return;
  }
  if (!writeRegister(REG_POLARITY0, 0) || !writeRegister(REG_POLARITY1, 0) ||
      !writeRegister(REG_CONFIG0, 0) || !writeRegister(REG_CONFIG1, 0) ||
      !writeRegister(REG_OUTPUT0, selectedMask)) {
    latchFault(F("configuration_write_failed"));
    return;
  }
  if (!readRegisters()) {
    latchFault(F("initial_readback_failed"));
    return;
  }
  if (!registersMatchMask(selectedMask)) {
    latchFault(F("initial_selection_mismatch"));
    return;
  }
  delay(SETTLE_MS);
  (void)analogRead(A2);  // discard first conversion after selection
  ready = true;
  lastCheckAt = millis();
  lastFrameAt = lastCheckAt;  // no sampling until at least FRAME_MS after settling
  Serial.print(F("# READY t=")); Serial.print(lastCheckAt);
  Serial.print(F(" addr=0x20 selected_bit=P2 analog=A2"));
  printRegisters();
  Serial.println(F(" verification=digital_only"));
}

void printSelectionIdentity() {
  Serial.print(F(" token=")); Serial.print(selectionToken);
  Serial.print(F(" channel=S")); Serial.print(selectedChannel);
}

void selectPressure(uint8_t channel, uint32_t token) {
  if (!ready || faultLatched) {
    Serial.println(F("# COMMAND rejected=not_ready action=I_then_SELECT"));
    return;
  }
  ready = false;
  clearZero(F("channel_select"));
  bool clear0 = writeRegister(REG_OUTPUT0, 0);
  uint8_t clearCode = lastWireCode;
  bool clearTimeout = lastWireTimeout;
  bool clear1 = writeRegister(REG_OUTPUT1, 0);
  if (!clear0 || !clear1) {
    if (!clear0) {
      lastRegister = REG_OUTPUT0;
      lastWireCode = clearCode;
      lastWireTimeout = clearTimeout;
    }
    latchFault(F("select_clear_failed")); return;
  }
  if (!readRegisters() || !registersMatchMask(0)) {
    latchFault(F("select_off_readback_failed")); return;
  }
  uint8_t nextMask = (uint8_t)(1u << (channel-1));
  if (!writeRegister(REG_OUTPUT0, nextMask)) {
    latchFault(F("select_write_failed")); return;
  }
  if (!readRegisters() || !registersMatchMask(nextMask)) {
    latchFault(F("select_on_readback_failed")); return;
  }
  delay(SETTLE_MS);
  (void)analogRead(A2);
  selectedChannel = channel;
  selectedMask = nextMask;
  selectionToken = token;
  ready = true;
  lastCheckAt = millis();
  lastFrameAt = lastCheckAt;
  Serial.print(F("# SELECT")); printSelectionIdentity();
  Serial.print(F(" mask=0x")); printHexByte(selectedMask);
  Serial.println(F(" adc=A2 verified=1"));
}

// Strict bounded uint32 decimal parser; no signs, suffixes, or silent overflow.
bool parseToken(const char *text, uint32_t &token) {
  if (!*text) return false;
  uint32_t result = 0;
  for (; *text; ++text) {
    if (*text < '0' || *text > '9') return false;
    uint8_t digit = (uint8_t)(*text-'0');
    if (result > (UINT32_MAX-digit)/10u) return false;
    result = result*10u+digit;
  }
  if (!result) return false;
  token = result;
  return true;
}

void requestZero() {
  if (!ready || faultLatched) {
    Serial.println(F("# COMMAND rejected=not_ready action=I_then_Z"));
    return;
  }
  baselineValid = false;
  baselineVoltage = 0.0f;
  baselineAt = 0;
  zeroCollecting = true;
  zeroCount = 0;
  zeroSum = 0.0f;
  zeroStartAt = 0;
  if (++zeroSequence == 0) ++zeroSequence;
  zeroId = zeroSequence;
  Serial.print(F("# ZERO_PENDING id=")); Serial.print(zeroId);
  Serial.print(F(" t=")); Serial.print(millis());
  Serial.println(F(" goal=40 previous_zero_cleared=1 stay_unloaded=1"));
}

void printStatus() {
  bool readOk = readRegisters();
  bool matches = readOk && registersMatchMask(selectedMask);
  if (ready && !matches) latchFault(readOk ? F("status_selection_mismatch") : F("status_readback_failed"));
  Serial.print(F("# STATUS t=")); Serial.print(millis());
  Serial.print(F(" ready=")); Serial.print(ready ? 1 : 0);
  Serial.print(F(" fault=")); Serial.print(faultLatched ? 1 : 0);
  printSelectionIdentity();
  Serial.print(F(" selection_match=")); Serial.print(matches ? 1 : 0);
  printRegisters();
  Serial.print(F(" id=")); Serial.print(zeroId);
  Serial.print(F(" zero=")); Serial.println(baselineValid ? F("set") : zeroCollecting ? F("collecting") : F("unset"));
}

void executeCommand() {
  commandBuffer[commandLength] = '\0';
  char *start = commandBuffer;
  while (*start == ' ' || *start == '\t') ++start;
  char *end = start + strlen(start);
  while (end > start && (end[-1] == ' ' || end[-1] == '\t')) --end;
  *end = '\0';
  for (char *c = start; *c; ++c) *c = (char)tolower((unsigned char)*c);
  if (!*start) return;
  // At most three whitespace-separated tokens. Never execute a valid prefix
  // of a malformed line; tokenize only after the complete line was validated.
  char *parts[4] = {};
  uint8_t count = 0;
  char *cursor = start;
  while (*cursor && count < 4) {
    parts[count++] = cursor;
    while (*cursor && *cursor != ' ' && *cursor != '\t') ++cursor;
    if (*cursor) *cursor++ = '\0';
    while (*cursor == ' ' || *cursor == '\t') ++cursor;
  }
  if (!strcmp(parts[0], "select")) {
    uint32_t token = 0;
    if (count != 3 || strlen(parts[1]) != 2 || parts[1][0] != 's' ||
        parts[1][1] < '1' || parts[1][1] > '5' || !parseToken(parts[2], token)) {
      Serial.println(F("# COMMAND rejected=invalid_select use=H"));
    } else selectPressure((uint8_t)(parts[1][1]-'0'), token);
    return;
  }
  bool zero = !strcmp(parts[0], "z") || !strcmp(parts[0], "zero");
  bool clear = !strcmp(parts[0], "c") || !strcmp(parts[0], "clear");
  if (zero || clear) {
    uint32_t token = selectionToken;
    if (count > 2 || (count == 2 && !parseToken(parts[1], token))) {
      Serial.println(F("# COMMAND rejected=invalid_token use=H")); return;
    }
    bool accepted = token == selectionToken && ready && !faultLatched;
    if (accepted) {
      if (zero) requestZero(); else clearZero(F("operator_clear"));
    }
    Serial.print(F("# COMMAND token=")); Serial.print(token);
    Serial.print(F(" channel=S")); Serial.print(selectedChannel);
    Serial.print(F(" op=")); Serial.print(zero ? F("zero") : F("clear"));
    Serial.print(F(" accepted=")); Serial.print(accepted ? 1 : 0);
    if (!accepted) Serial.print(token != selectionToken ? F(" reason=token_mismatch") : F(" reason=not_ready"));
    Serial.println();
    return;
  }
  if (count != 1) { Serial.println(F("# COMMAND rejected=unknown use=H")); return; }
  if (!strcmp(start, "d") || !strcmp(start, "status")) printStatus();
  else if (!strcmp(start, "i") || !strcmp(start, "init")) initializeSelection(false);
  else if (!strcmp(start, "h") || !strcmp(start, "help")) printHelp();
  else Serial.println(F("# COMMAND rejected=unknown use=H"));
}

void pollCommands() {
  // A bounded amount of command work each loop; no String heap allocations.
  for (uint8_t n = 0; n < 32 && Serial.available(); ++n) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (commandInvalid) Serial.println(F("# COMMAND rejected=invalid_character use=H"));
      else if (commandOverflow) Serial.println(F("# COMMAND rejected=too_long use=H"));
      else executeCommand();
      commandLength = 0;
      commandOverflow = false;
      commandInvalid = false;
    } else if (((uint8_t)c < 0x20 && c != '\t') || (uint8_t)c > 0x7e) {
      // Do not let NUL or other controls truncate an unknown command into Z/I.
      commandInvalid = true;
    } else if (!commandOverflow && !commandInvalid) {
      if (commandLength < sizeof(commandBuffer)-1) commandBuffer[commandLength++] = c;
      else commandOverflow = true;
    }
  }
}

void sampleFrame() {
  uint16_t sum = 0;
  for (uint8_t i = 0; i < ADC_COUNT; ++i) sum += analogRead(A2);
  unsigned long at = millis();
  lastFrameAt = at;  // next sample starts >=100 ms after this sample finishes
  float adcMean = sum / (float)ADC_COUNT;
  float voltage = adcMean * NOMINAL_VREF / 1023.0f;
  bool zeroCompleted = false;
  if (zeroCollecting) {
    if (zeroCount == 0) zeroStartAt = at;
    zeroSum += voltage;
    ++zeroCount;
    if (zeroCount == ZERO_FRAMES) {
      baselineVoltage = zeroSum / ZERO_FRAMES;
      baselineAt = at;
      baselineValid = true;
      zeroCollecting = false;
      zeroCompleted = true;
    }
  }
  if (zeroCompleted) {
    Serial.print(F("# ZERO_SET id=")); Serial.print(zeroId);
    Serial.print(F(" t=")); Serial.print(baselineAt);
    Serial.print(F(" start=")); Serial.print(zeroStartAt);
    Serial.print(F(" n=40 V0=")); Serial.println(baselineVoltage, 4);
  }
  // Every row identifies its selection handshake and baseline generation.
  // Absolute nominal voltage is never baseline-subtracted in the V column.
  Serial.print(F("P,")); Serial.print(at);
  Serial.print(F(",S")); Serial.print(selectedChannel);
  Serial.print(','); Serial.print(adcMean, 1);
  Serial.print(','); Serial.print(voltage, 4);
  Serial.print(',');
  if (baselineValid) Serial.print(baselineVoltage, 4); else Serial.print(F("NA"));
  Serial.print(',');
  if (baselineValid) Serial.print(voltage-baselineVoltage, 4); else Serial.print(F("NA"));
  Serial.print(','); Serial.print(zeroId);
  Serial.print(',');
  if (baselineValid) Serial.print(baselineAt); else Serial.print(F("NA"));
  Serial.print(','); Serial.print(zeroCount);
  Serial.print(','); Serial.println(selectionToken);
}

void setup() {
  Serial.begin(9600);
  Wire.begin();
  Wire.setWireTimeout(25000UL, true);
  Serial.println(F("# Pressure_Diagnostic v2; pressure_v2; nominal_Vref=5.0; ADC=A2; samples=10; frame_ms>=100"));
  printHelp();
  initializeSelection(true);
}

void loop() {
  pollCommands();
  if (!ready || faultLatched) return;
  unsigned long now = millis();
  if ((unsigned long)(now-lastCheckAt) >= CHECK_MS) {
    lastCheckAt = now;
    if (!readRegisters()) { latchFault(F("periodic_readback_failed")); return; }
    if (!registersMatchMask(selectedMask)) { latchFault(F("periodic_selection_mismatch")); return; }
  }
  now = millis();
  if ((unsigned long)(now-lastFrameAt) >= FRAME_MS) {
    lastFrameAt = now;
    sampleFrame();
  }
}
