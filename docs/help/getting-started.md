# Getting Started

## 1. Know your hardware

- **HL2** (plain) — stock Hermes Lite 2 board. RX audio is decoded on
  the PC and played through the PC sound system (default audio device
  or whatever you pick in **DSP & AUDIO → Output**).
- **HL2+** — HL2 base **plus** the AK4951 audio add-in board. RX audio
  can be routed via EP2 → AK4951 → phones/line jack → PC line-in for
  lower-latency hardware monitoring. TX uses the AK4951 microphone
  input. Requires the updated HL2+ gateware.

## 2. Network

The HL2 is a Layer-2 Ethernet device using the HPSDR Protocol-1 on UDP
port 1024. It must be reachable on your local subnet. Typical setups:

- **Direct Ethernet** to the PC — simplest, no switch needed.
- **Same LAN as PC** — any gigabit switch is fine.
- **Across routers** — not supported. P1 discovery is broadcast-only.

Make sure Windows Firewall allows inbound UDP 1024 for `python.exe`
(or whatever you've packaged Lyra as).

## 3. First launch

1. Toolbar → **⚙ Settings…** → **Radio** tab.
2. Click **Discover** — any HL2 on the subnet will appear. Pick yours
   or paste the IP manually.
3. **Network/TCI** tab — default TCI port is 40001. Leave TCI disabled
   for now unless you have logging software to connect.
4. **Hardware** tab — enable the N2ADR filter board if you have one
   (and only if you do; otherwise the OC outputs drive nothing and
   it's harmless but unnecessary).
5. **Audio** tab — pick your output device, or leave as **Default**.
6. Close Settings.

## 4. Fire it up

- Toolbar → **▶ Start**. Status dot goes green.
- You should see a spectrum trace and hear a noise floor.
- If you don't: see **Troubleshooting**.

## 5. Save your workspace

The panel layout (which panels are visible, where they dock, floating
window positions) is saved automatically on close and restored on next
launch.

**View → Reset Panel Layout** restores the factory arrangement if you
end up with panels somewhere weird.
