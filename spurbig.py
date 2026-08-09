#!/usr/bin/env python3
"""
Single-Purpose Bootable Image Generator for Ubuntu 26
Generates a minimal, bootable Linux ISO targeting a single dynamically linked binary.
"""

import argparse
import glob
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


class Color:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def log_info(msg: str):
    print(f"{Color.OKBLUE}[INFO]{Color.ENDC} {msg}")

def log_success(msg: str):
    print(f"{Color.OKGREEN}[SUCCESS]{Color.ENDC} {msg}")

def log_warn(msg: str):
    print(f"{Color.WARNING}[WARN]{Color.ENDC} {msg}")

def log_error(msg: str):
    print(f"{Color.FAIL}[ERROR]{Color.ENDC} {msg}", file=sys.stderr)

def run_cmd(cmd: list[str], check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=check, text=True, capture_output=capture_output)
    except subprocess.CalledProcessError as e:
        log_error(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr}")
        raise


class BootImageGenerator:
    def __init__(self, args: argparse.Namespace):
        self.binary_path = Path(args.binary).resolve()
        self.output_iso = Path(args.output).resolve()
        self.include_graphics = args.include_graphics
        self.include_audio = args.include_audio
        self.custom_files = args.add_file or []
        self.dry_run_strace = args.dry_run_strace

        self.kernel_ver = os.uname().release
        self.kernel_path = Path(f"/boot/vmlinuz-{self.kernel_ver}")
        self.work_dir = Path(tempfile.mkdtemp(prefix="bootgen_"))
        self.staging_dir = self.work_dir / "rootfs"
        
        self.libs_to_copy: set[Path] = set()
        self.modules_to_copy: set[str] = set()
        self.firmware_to_copy: set[Path] = set()

    def cleanup(self):
        log_info(f"Cleaning up temporary directory: {self.work_dir}")
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def validate_host_environment(self):
        log_info("Validating host environment...")
        if os.geteuid() != 0:
            raise PermissionError("This tool must be run as root (sudo) to construct device nodes and mount ISO structures.")

        if not self.binary_path.exists() or not os.access(self.binary_path, os.X_OK):
            raise FileNotFoundError(f"Target binary not found or not executable: {self.binary_path}")

        if not self.kernel_path.exists():
            raise FileNotFoundError(f"Host kernel not found: {self.kernel_path}")

        required_tools = ["ldd", "cpio", "gzip", "grub-mkrescue", "depmod", "modinfo"]
        if shutil.which("busybox") is None:
            required_tools.append("busybox")
            
        for tool in required_tools:
            if shutil.which(tool) is None:
                raise RuntimeError(f"Required utility '{tool}' is missing. Install it before running.")

    # -------------------------------------------------------------------------
    # Stage 1: Dependency Resolution
    # -------------------------------------------------------------------------
    def resolve_elf_dependencies(self, binary: Path):
        log_info(f"Tracing ELF dependencies for {binary}...")
        res = run_cmd(["ldd", str(binary)], check=False)
        if res.returncode != 0:
            log_warn(f"ldd returned non-zero for {binary}. Might be statically linked or missing loader.")
            return

        for line in res.stdout.splitlines():
            line = line.strip()
            if "=>" in line:
                parts = line.split("=>")
                right = parts[1].strip().split()[0]
                if os.path.isabs(right) and os.path.exists(right):
                    self.libs_to_copy.add(Path(right))
            elif line.startswith("/"):
                path_str = line.split()[0]
                if os.path.exists(path_str):
                    self.libs_to_copy.add(Path(path_str))

    def resolve_package_files(self, sample_file: str) -> set[Path]:
        """Package-Aware Resolution using dpkg heuristics."""
        found_files = set()
        if shutil.which("dpkg") is None or not os.path.exists(sample_file):
            return found_files

        res = run_cmd(["dpkg", "-S", sample_file], check=False)
        if res.returncode == 0:
            pkg_name = res.stdout.split(":")[0].strip()
            log_info(f"Heuristic: Identified package '{pkg_name}' for '{sample_file}'")
            pkg_res = run_cmd(["dpkg", "-L", pkg_name], check=False)
            if pkg_res.returncode == 0:
                for p in pkg_res.stdout.splitlines():
                    path = Path(p.strip())
                    if path.is_file() and not path.is_symlink():
                        found_files.add(path)
        return found_files

    def resolve_subsystems(self):
        # 1. Input Subsystem Libraries
        input_libs = ["libudev.so", "libevdev.so", "libxkbcommon.so"]
        for lib in input_libs:
            for search_path in ["/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu"]:
                matches = glob.glob(os.path.join(search_path, f"{lib}*"))
                for match in matches:
                    self.libs_to_copy.add(Path(match))

        # 2. Dynamic Strace Dry-Run
        if self.dry_run_strace:
            log_info("Executing binary dry-run with strace to detect dynamic dlopen accesses...")
            strace_out = self.work_dir / "strace.log"
            cmd = ["strace", "-f", "-e", "trace=open,openat", "-o", str(strace_out), str(self.binary_path)]
            log_warn("Running target binary briefly. Terminate/close target app if it blocks...")
            subprocess.run(cmd, timeout=5, check=False)
            if strace_out.exists():
                with open(strace_out, "r", errors="ignore") as f:
                    for line in f:
                        m = re.search(r'"(/usr/lib/|/lib/|/usr/share/)[^"]+"', line)
                        if m:
                            p = Path(m.group(0).strip('"'))
                            if p.is_file():
                                self.libs_to_copy.add(p)

        # 3. Graphics (Mesa/DRM) Subsystem
        if self.include_graphics:
            log_info("Applying Graphics (Mesa/DRM) Profile...")
            graphics_dirs = [
                "/usr/lib/x86_64-linux-gnu/dri/",
                "/usr/lib/x86_64-linux-gnu/gallium-pipe/",
                "/usr/share/vulkan/icd.d/",
                "/usr/share/glvnd/",
            ]
            for gdir in graphics_dirs:
                if os.path.exists(gdir):
                    for root, _, files in os.walk(gdir):
                        for f in files:
                            self.libs_to_copy.add(Path(root) / f)

            patterns = [
                "/usr/lib/x86_64-linux-gnu/libGLX_*.so*",
                "/usr/lib/x86_64-linux-gnu/libEGL*",
                "/usr/lib/x86_64-linux-gnu/libgbm*",
                "/usr/lib/x86_64-linux-gnu/libdrm*",
            ]
            for pat in patterns:
                for match in glob.glob(pat):
                    self.libs_to_copy.add(Path(match))

            # Trigger package-aware lookup fallback on core libGL
            sample_gl = "/usr/lib/x86_64-linux-gnu/libGL.so.1"
            if os.path.exists(sample_gl):
                self.libs_to_copy.update(self.resolve_package_files(sample_gl))

        # 4. Audio (ALSA) Subsystem
        if self.include_audio:
            log_info("Applying Audio (ALSA) Profile...")
            audio_dirs = [
                "/usr/lib/x86_64-linux-gnu/alsa-lib/",
                "/usr/share/alsa/",
                "/var/lib/alsa/",
            ]
            for adir in audio_dirs:
                if os.path.exists(adir):
                    for root, _, files in os.walk(adir):
                        for f in files:
                            self.libs_to_copy.add(Path(root) / f)

            sample_alsa = "/usr/lib/x86_64-linux-gnu/libasound.so.2"
            if os.path.exists(sample_alsa):
                self.libs_to_copy.update(self.resolve_package_files(sample_alsa))

    # -------------------------------------------------------------------------
    # Stage 2: Root Filesystem (rootfs) Construction
    # -------------------------------------------------------------------------
    def build_rootfs_structure(self):
        log_info("Constructing staging rootfs directory structure...")
        dirs = [
            "bin", "sbin", "etc", "proc", "sys", "dev", "dev/pts", "dev/input",
            "lib", "lib64", "usr/bin", "usr/sbin", "usr/lib", "usr/share",
            "tmp", "var/lib/alsa", "run"
        ]
        for d in dirs:
            (self.staging_dir / d).mkdir(parents=True, exist_ok=True)

        # Deploy Busybox
        busybox_host = shutil.which("busybox")
        target_busybox = self.staging_dir / "bin" / "busybox"
        shutil.copy2(busybox_host, target_busybox)
        target_busybox.chmod(0o755)

        # Create Busybox Symlinks
        symlinks = ["sh", "ash", "ls", "mount", "umount", "mkdir", "mdev", "modprobe", "depmod", "cat", "echo", "sleep", "clear"]
        for sym in symlinks:
            link_path = self.staging_dir / "bin" / sym
            if not link_path.exists():
                link_path.symlink_to("busybox")

        # Copy Target Binary
        dest_binary = self.staging_dir / self.binary_path.relative_to("/")
        dest_binary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.binary_path, dest_binary)
        dest_binary.chmod(0o755)

        # Copy Custom Injected Files
        for item in self.custom_files:
            if ":" not in item:
                log_warn(f"Invalid --add-file syntax '{item}'. Expected host_path:image_path")
                continue
            h_path, i_path = item.split(":", 1)
            host_p, img_p = Path(h_path).resolve(), Path(i_path.lstrip("/"))
            target_p = self.staging_dir / img_p
            target_p.parent.mkdir(parents=True, exist_ok=True)
            if host_p.is_dir():
                shutil.copytree(host_p, target_p, dirs_exist_ok=True)
            else:
                shutil.copy2(host_p, target_p)

        # Copy All Resolved Libraries and Assets
        log_info(f"Copying {len(self.libs_to_copy)} resolved library files into rootfs...")
        for lib in self.libs_to_copy:
            if not lib.exists():
                continue
            # Handle real paths vs symlinks
            real_lib = lib.resolve()
            rel_path = real_lib.relative_to("/")
            target_lib = self.staging_dir / rel_path
            target_lib.parent.mkdir(parents=True, exist_ok=True)
            if not target_lib.exists():
                shutil.copy2(real_lib, target_lib)

            # Re-create symlink if lib was a symlink
            if lib.is_symlink():
                link_rel_path = lib.relative_to("/")
                target_link = self.staging_dir / link_rel_path
                target_link.parent.mkdir(parents=True, exist_ok=True)
                if not target_link.exists():
                    target_link.symlink_to(real_lib)

        # Configure mdev rules for device nodes
        mdev_conf = self.staging_dir / "etc" / "mdev.conf"
        mdev_conf.write_text(
            "event.* 0:0 660 =input/\n"
            "js.*    0:0 660 =input/\n"
            "mice    0:0 660 =input/\n"
            "mouse.* 0:0 660 =input/\n"
            "dri/.*  0:0 666 =dri/\n"
            "snd/.*  0:0 666 =snd/\n"
        )

    # -------------------------------------------------------------------------
    # Stage 3: Kernel Modules and Firmware
    # -------------------------------------------------------------------------
    def collect_modules_and_firmware(self):
        log_info(f"Gathering modules and firmware for kernel {self.kernel_ver}...")
        
        # Base input subsystem modules
        modules = [
            "usbcore", "xhci-hcd", "xhci-pci", "ehci-pci", 
            "usbhid", "hid", "hid-generic", "evdev", "mousedev", "joydev",
            "xpad", "hid-sony", "hid-playstation", "hid-nintendo"
        ]

        if self.include_graphics:
            modules.extend(["drm", "drm_kms_helper", "amdgpu", "i915", "nouveau", "virtio_gpu", "vmwgfx", "radeon"])

        if self.include_audio:
            modules.extend(["snd", "snd-pcm", "snd-timer", "snd-hda-intel", "snd-hda-codec-hdmi", "snd-hda-codec-realtek"])

        # Determine module paths recursively via modinfo
        copied_modules = set()
        for mod in modules:
            res = run_cmd(["modinfo", "-k", self.kernel_ver, "-F", "filename", mod], check=False)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    mod_file = Path(line.strip())
                    if mod_file.exists() and mod_file not in copied_modules:
                        copied_modules.add(mod_file)
                        
                        # Check firmware requests via modinfo
                        fw_res = run_cmd(["modinfo", "-k", self.kernel_ver, "-F", "firmware", mod], check=False)
                        if fw_res.returncode == 0:
                            for fw in fw_res.stdout.splitlines():
                                fw_path = Path("/lib/firmware") / fw.strip()
                                if fw_path.exists():
                                    self.firmware_to_copy.add(fw_path)

        # Copy modules preserving hierarchy
        for mod_file in copied_modules:
            rel = mod_file.relative_to("/")
            target = self.staging_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mod_file, target)

        # Copy Firmware files
        for fw in self.firmware_to_copy:
            rel = fw.relative_to("/")
            target = self.staging_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if fw.is_dir():
                shutil.copytree(fw, target, dirs_exist_ok=True)
            else:
                shutil.copy2(fw, target)

        # Generate depmod indexes in rootfs
        log_info("Generating module dependencies (depmod)...")
        run_cmd(["depmod", "-b", str(self.staging_dir), self.kernel_ver], check=False)

    # -------------------------------------------------------------------------
    # Stage 4: Init Script Generation
    # -------------------------------------------------------------------------
    def generate_init_script(self):
        log_info("Generating executable /init script...")
        init_path = self.staging_dir / "init"
        
        target_binary_rel = "/" + str(self.binary_path.relative_to("/"))

        init_script_content = f"""#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
export ALSA_CONFIG_DIR=/usr/share/alsa

# 1. Mount virtual filesystems
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev

mkdir -p /dev/pts /dev/shm /dev/input
mount -t devpts devpts /dev/pts

# 2. Register mdev hotplug listener
echo /bin/mdev > /proc/sys/kernel/hotplug
mdev -s

# 3. Load input, graphics, and audio modules
modprobe -a usbcore xhci-hcd xhci-pci usbhid hid hid-generic evdev mousedev joydev 2>/dev/null
modprobe -a drm amdgpu i915 nouveau virtio_gpu 2>/dev/null
modprobe -a snd snd-pcm snd-hda-intel 2>/dev/null

# 4. Secondary mdev scan after module load
mdev -s

echo "===================================================="
echo " Single-Purpose System Booted Successfully"
echo " Executing application: {target_binary_rel}"
echo "===================================================="

# 5. Execute target binary replacing PID 1
exec {target_binary_rel}

# Fallback if binary fails
echo "ERROR: Target application exited or failed to run!"
exec /bin/sh
"""
        init_path.write_text(init_script_content)
        init_path.chmod(0o755)

    # -------------------------------------------------------------------------
    # Stage 5: Image Generation & Bootloader
    # -------------------------------------------------------------------------
    def build_iso(self):
        log_info("Compressing rootfs into initramfs cpio archive...")
        initramfs_img = self.work_dir / "initramfs.cpio.gz"
        
        # Build cpio archive safely
        cmd_find = subprocess.Popen(["find", ".", "-print0"], cwd=self.staging_dir, stdout=subprocess.PIPE)
        cmd_cpio = subprocess.Popen(["cpio", "--null", "-ov", "-H", "newc"], cwd=self.staging_dir, stdin=cmd_find.stdout, stdout=subprocess.PIPE)
        cmd_gzip = subprocess.Popen(["gzip", "-9"], stdin=cmd_cpio.stdout, stdout=open(initramfs_img, "wb"))
        
        cmd_find.stdout.close()
        cmd_cpio.stdout.close()
        cmd_gzip.communicate()

        # Prepare GRUB ISO structure
        iso_root = self.work_dir / "iso_root"
        boot_grub = iso_root / "boot" / "grub"
        boot_grub.mkdir(parents=True, exist_ok=True)

        shutil.copy2(self.kernel_path, iso_root / "boot" / "vmlinuz")
        shutil.copy2(initramfs_img, iso_root / "boot" / "initramfs.cpio.gz")

        grub_cfg = boot_grub / "grub.cfg"
        grub_cfg.write_text(
            'set default=0\n'
            'set timeout=0\n\n'
            'menuentry "Standalone App" {\n'
            '    linux /boot/vmlinuz quiet loglevel=3 raw\n'
            '    initrd /boot/initramfs.cpio.gz\n'
            '}\n'
        )

        log_info(f"Generating bootable ISO with grub-mkrescue at {self.output_iso}...")
        self.output_iso.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["grub-mkrescue", "-o", str(self.output_iso), str(iso_root)])

    def run(self):
        try:
            self.validate_host_environment()
            
            # Step 1: Trace dynamic libraries
            self.resolve_elf_dependencies(self.binary_path)
            self.resolve_subsystems()

            # Step 2 & 3: Construct rootfs and dependencies
            self.build_rootfs_structure()
            self.collect_modules_and_firmware()

            # Step 4: Write Init logic
            self.generate_init_script()

            # Step 5: Package ISO
            self.build_iso()

            log_success(f"Bootable ISO successfully generated at: {self.output_iso}")

        finally:
            self.cleanup()


def main():
    parser = argparse.ArgumentParser(
        description="Packager that creates minimalistic bootable ISOs from dynamically linked binaries."
    )
    parser.add_argument("--binary", required=True, help="Path to executable binary")
    parser.add_argument("--output", required=True, help="Output .iso file path")
    parser.add_argument("--include-graphics", action="store_true", help="Bundle Mesa/DRM drivers and firmware")
    parser.add_argument("--include-audio", action="store_true", help="Bundle ALSA plugins and kernel modules")
    parser.add_argument("--add-file", action="append", help="Inject custom files formatted as <host_path>:<image_path>")
    parser.add_argument("--dry-run-strace", action="store_true", help="Run binary under strace to trace dynamic dlopen dependencies")

    args = parser.parse_args()

    try:
        generator = BootImageGenerator(args)
        generator.run()
    except Exception as e:
        log_error(f"Execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()