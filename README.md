# Quadify Plugin

The **Quadify Plugin** integrates advanced audio display and control hardware with Volumio.
It provides seamless support for OLED displays, rotary encoders, buttons, and LEDs on Raspberry Pi systems running Volumio.

## Features

* OLED display integration (SSD1322 and other types supported via `luma.oled`)
* Rotary encoder support (volume and navigation)
* Button and LED input/output
* CAVA visualiser (VU meter mode)
* Modular hardware configuration via `config.yaml`

## Requirements

* A Raspberry Pi running **Volumio 3 or later**
* Active internet connection during installation (to fetch dependencies)
* Basic hardware setup (OLED, encoder, buttons, LEDs connected as per `config.yaml`)

---

## Installation

**Step 1.** SSH into your Volumio device:

```bash
ssh volumio@volumio.local
```

**Step 2.** Clone (download) this repository:

```bash
git clone https://github.com/theshepherdmatt/Quadify-Plugin.git
cd Quadify-Plugin
```

**Step 3.** Install the plugin with Volumio:

```bash
volumio plugin install
```

**Step 4.** Enable the plugin in the Volumio Web UI under:
`Plugins → Installed Plugins → Quadify Plugin → Enable`

---

## Managing the Plugin

**Restart the plugin service:**

```bash
sudo systemctl restart quadify
```

**Follow plugin logs:**

```bash
journalctl -u quadify -f
```

**Check CAVA (visualiser) status:**

```bash
sudo systemctl status cava
```

---

## Debugging

If Quadify Plugin isn’t working as expected:

1. Check Volumio logs:

   ```bash
   journalctl -u volumio -f
   ```
2. Run the Python backend directly for errors:

   ```bash
   cd /data/plugins/system_hardware/quadify/quadifyapp/src
   python3 main.py
   ```

If issues persist, please [open an Issue](https://github.com/theshepherdmatt/Quadify-Plugin/issues) with your setup details and error messages.

Perfect — you’ve got all the content, it just needs **clean hierarchy and consistent formatting** so each screen looks like a mini section with **header → image → description**. Here’s a polished rewrite of your “Display Screens” section in that format:

---

## Display Screens

Quadify provides a series of screens to guide you from startup through playback.

---

### **1. Loading Screen**

<img width="768" height="192" alt="logo" src="https://github.com/user-attachments/assets/6f021e9c-35b4-46e1-8510-e662c7d64633" />  

Shown while the Quadify service initialises.
Confirms the plugin and OLED display are starting correctly.

---

### **2. Ready Screen**

<img width="768" height="192" alt="ready" src="https://github.com/user-attachments/assets/a3460598-44a5-43d4-9f4c-16e4a11b80e9" />  

Indicates that Quadify is active but idle (no music yet).
Press any button, turn the encoder, or use your remote to exit to the menu.

---

### **3. Menu Screen**

<img width="762" height="192" alt="menu" src="https://github.com/user-attachments/assets/cc0f896c-f56a-4514-abad-a1c22c4e99a9" />  

Entry point for choosing a display mode.
Categories: **Modern**, **VU**, **Clock**.

---

### **4. Playing Screens**

Shows playback information and visualisations while music plays.

#### Modern Visualisers

**Bars** – vertical spectrum bars <img width="762" height="192" alt="modernbars" src="https://github.com/user-attachments/assets/1fca2619-6378-4923-872d-239019a5001f" />

**Dots** – point-based spectrum <img width="762" height="192" alt="spots" src="https://github.com/user-attachments/assets/68a474d1-ccf9-41d1-af80-4c7242697ea2" />

**Oscilloscope** – waveform trace <img width="762" height="192" alt="osci" src="https://github.com/user-attachments/assets/82e023ba-7d91-4c7c-9126-2db421f73c20" />

#### VU Meters

**Classic** – retro dual-needle meter
**Digital** – digital peak meter *(coming soon)* <img width="762" height="192" alt="osci" src="https://github.com/user-attachments/assets/96266694-0607-4041-936d-36141edcb453" />

#### Clock

**Clock** – simple digital time display (text-based).

---

Would you like me to also add a **table of screens** at the start of this section (like a quick reference with thumbnails + one-line descriptions), so users can skim before scrolling into the detailed breakdown? That might make the README even more user-friendly.


Display Screens

Quadify provides a series of screens to guide you from startup through playback.


<img width="768" height="192" alt="logo" src="https://github.com/user-attachments/assets/6f021e9c-35b4-46e1-8510-e662c7d64633" />

1. Loading Screen

Shown while the Quadify service initialises.

Confirms the plugin and OLED display are starting correctly.



<img width="768" height="192" alt="ready" src="https://github.com/user-attachments/assets/a3460598-44a5-43d4-9f4c-16e4a11b80e9" />

2. Ready Screen

Indicates that Quadify is active but idle (no music yet).

Press any button, turn the encoder, or use your remote to exit to the menu.



<img width="762" height="192" alt="menu" src="https://github.com/user-attachments/assets/cc0f896c-f56a-4514-abad-a1c22c4e99a9" />

3. Menu Screen

Entry point for choosing a display mode.

Categories: Modern, VU, Clock.



<img width="762" height="192" alt="modernbars" src="https://github.com/user-attachments/assets/1fca2619-6378-4923-872d-239019a5001f" />

4. Playing Screens

Shows playback information and visualisations while music plays.

Modern Visualisers

Bars – vertical spectrum bars



<img width="762" height="192" alt="osci" src="https://github.com/user-attachments/assets/82e023ba-7d91-4c7c-9126-2db421f73c20" />
Dots – point-based spectrum


<img width="762" height="192" alt="spots" src="https://github.com/user-attachments/assets/68a474d1-ccf9-41d1-af80-4c7242697ea2" />
Oscilloscope – waveform trace

<img width="762" height="192" alt="osci" src="https://github.com/user-attachments/assets/96266694-0607-4041-936d-36141edcb453" />
VU Meters

Classic – retro dual-needle meter

Digital – digital peak meter (coming soon)
