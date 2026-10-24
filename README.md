<p align="center">
  <img src="pictures/OpenNept4une.png" width="350" alt="OpenNept4une Logo">
  <h1 align="center">OpenNept4une</h1>
  <h1 align="center">De-Elegoo-izing the Neptune 4 Series 3D Printers</h1>
</p>

## About this fork

This is a fork of [OpenNeptune3D/OpenNept4une](https://github.com/OpenNeptune3D/OpenNept4une),
tested on the Neptune 4 Max with the stock Elegoo screen firmware 1.2.22. It
follows upstream and adds:

- **Power-loss monitor for the Plus/Max.** It reads Elegoo's power-sense pin
  and shuts the board down cleanly while the super-capacitor still powers it
  ([docs/power-monitor.md](docs/power-monitor.md)).
- **Power-loss recovery.** When the power fails during a print, the state is
  saved and the print stopped. After the power returns, the screen offers to
  continue from the start of the G-code line that was printing, with the same
  bed mesh (including adaptive meshes), at reduced speed for the rest of that
  layer. The **Power Loss Recovery** switch in the screen's advanced settings
  turns it on and off ([docs/power-loss-recovery.md](docs/power-loss-recovery.md)).
- **schedutil CPU governor.** The kernel starts in performance, which keeps
  the CPU at its top speed even while idle; `cpu-governor.service`
  (`img-config/cpu-governor.sh`) switches it to schedutil at boot.
- **Its own display connector**,
  [huzhekun/openneptune_display_connector](https://github.com/huzhekun/openneptune_display_connector):
  on-screen Wi-Fi setup, the power-loss recovery prompt and switch, screen
  sleep, and fixes for the stock screen firmware 1.2.22.

The script and Moonraker's update manager update each repository from the
remote its checkout already tracks (`origin`), so a printer set up from these
forks keeps updating from them, and one set up from upstream stays on
upstream. The forks are only the default for a new checkout. The Releases,
Wiki and Discord links below are upstream's.

### Switching an existing install to this fork

A printer already running OpenNept4une can switch without flashing anything:
the forks work with the stock Elegoo screen firmware. Over SSH, as `mks`:

```sh
# OpenNept4une
cd ~/OpenNept4une
git remote set-url origin https://github.com/huzhekun/OpenNept4une.git
git fetch origin
git checkout -B main origin/main

# Display connector
cd ~/display_connector
git remote set-url origin https://github.com/huzhekun/openneptune_display_connector.git
git fetch origin
git checkout -B main origin/main
sudo sh ~/display_connector/wifi-permissions-setup.sh mks

# Installs the services and points Moonraker's update manager at the forks
~/OpenNept4une/OpenNept4une.sh update_repo
sudo reboot
```

`wifi-permissions-setup.sh` lets the display service set up Wi-Fi. The script
run installs the power monitor, power-loss recovery and `cpu-governor.service`,
adds `[include plr/*.cfg]` to `printer.cfg`, and rewrites the
`[update_manager OpenNept4une]` and `[update_manager display]` sections of
`moonraker.conf`, so updates from Mainsail or Fluidd come from the forks.

`git checkout -B main origin/main` resets the local `main` branch to the
fork's, so back up any local commits or changes in those two directories
first. The power monitor and power-loss recovery work on the Plus and Max
only. The screen features are tested with firmware 1.2.22; the connector also
accepts Elegoo 1.2.11 to 1.2.18.

To switch back, set the remotes to
`https://github.com/OpenNeptune3D/OpenNept4une.git` and
`https://github.com/OpenNeptune3D/display_connector.git`, check out their
branch the same way, and run `OpenNept4une.sh update_repo` again.

---

<div align="center">

| 📥 Download | ⚙️ Installation | 💬 Discord Server |
|---------------------------------------------|---------------------------------------------|--------------------------------------------|
| [Releases](https://github.com/OpenNeptune3D/OpenNept4une/releases/) | [Wiki](https://github.com/OpenNeptune3D/OpenNept4une/wiki) | [Discord](https://discord.gg/zX35xfvA2W) |

</div>

---

<div align="center">
  
### Credits: (Community Members)  
Phillip Thelen, barrenechea, SQUIRRELYMOOSE, DanDonut & Jaerax  
And our GitHub [Contributors](https://github.com/OpenNeptune3D/OpenNept4une/graphs/contributors)


</div>

**NOTE:** While the latest release file may appear outdated, this project is actively maintained and updated regularly. The main Linux image within the release includes a built-in updater that continuously pulls the latest changes directly from the project’s various GitHub repositories. This ensures that you always have the most recent updates, even if the release file itself hasn’t been changed in a while. Check the commit history for the latest improvements and fixes:

- [OpenNeptune3D/OpenNept4une commits](https://github.com/OpenNeptune3D/OpenNept4une/commits/dev)
- [OpenNeptune3D/display_connector commits](https://github.com/OpenNeptune3D/display_connector/commits/dev)

---
### Image Features
- Support for all Neptune 4 series models 
- Custom Armbian Image (25.11 trixie with Linux kernel 6.16.12) ([Credit](https://github.com/redrathnure/armbian-mkspi.git)) ([Credit](https://github.com/halfmanbear/Armbian-ZNP-K1-build))
- Klipper v0.13 installed + Easy MCU flash 
- Updatable services, packages & firmware (No need for Elegoo Firmware Updates going forward)
- TouchScreen Support BETA (More responsive)
- All Elegoo Services Removed (No Z-Axis Issues)
- Orca Slicer Profiles Provided
- Adaptive Meshing Configured
- Support for many for USB WiFi adapters 
- Simplified and highly modified printer.cfg
- Preconfigured LED, Bed Leveling & PID Calibration Macros
- Screws Tilt Adjust Configured 
- Axis Twist Compensation Configured
- Firmware Retraction configured
- Linux Host CPU & MCU temperature monitoring in Fluidd
- Improved mainboard cooling/fan control
- Simple mjpg-streamer webcam configuration script for MJPG & YUYV webcams
---

<div style="text-align: center;">
  <img src="pictures/main-page.png" width="450" alt="OpenNept4une Main-Page" style="margin-right: 10px;">
  <img src="pictures/advanced-page.png" width="450" alt="OpenNept4une Advanced-Page">
</div>

---

### Included Projects: 
  - [armbian](https://github.com/armbian/build)
  - [(Fork) armbian-mkspi](https://github.com/redrathnure/armbian-mkspi.git)
  - [(Fork) Armbian-ZNP-K1-build base image](https://github.com/OpenNeptune3D/Armbian-ZNP-K1-build)
  - [display_connector](https://github.com/OpenNeptune3D/display_connector)
  - [KAMP (Klipper-Adaptive-Meshing-Purging)](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
  - [kiauh (Klipper Installation And Update Helper)](https://github.com/dw-0/kiauh)
  - [Klipper](https://github.com/Klipper3d/klipper)
  - [moonraker](https://github.com/Arksine/moonraker)
  - [fluidd:](https://github.com/fluidd-core/fluidd)
  - [mainsail](https://github.com/mainsail-crew/mainsail)
  - [mobileraker](https://github.com/Clon1998/mobileraker)


