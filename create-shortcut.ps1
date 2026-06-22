# create-shortcut.ps1
# Creates a "Claude Traffic Light.lnk" shortcut on the Desktop pointing to
# the launcher app (launcher.pyw) with the traffic-light icon.
# After it runs, right-click the Desktop shortcut -> "Pin to taskbar".

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LauncherPyw = Join-Path $ScriptDir "bin\launcher.pyw"
$IconPath    = Join-Path $ScriptDir "bin\traffic_light.ico"
$ShortcutDst = [System.IO.Path]::Combine(
    [Environment]::GetFolderPath("Desktop"),
    "Claude Traffic Light.lnk"
)

# Find pythonw.exe so the shortcut runs without a console window.
$PythonDir = & py -3 -c "import sys,os; print(os.path.dirname(sys.executable))" 2>$null
$PythonW   = Join-Path $PythonDir "pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $PythonW = (Get-Command pythonw.exe -ErrorAction SilentlyContinue)?.Source
}
if (-not $PythonW -or -not (Test-Path $PythonW)) {
    Write-Warning "pythonw.exe not found — falling back to python.exe (console will flash briefly)"
    $PythonW = (Get-Command python.exe -ErrorAction SilentlyContinue)?.Source
}
if (-not $PythonW) {
    Write-Error "Python not found in PATH. Install Python 3 and try again."
    exit 1
}

$WShell   = New-Object -ComObject WScript.Shell
$Shortcut = $WShell.CreateShortcut($ShortcutDst)
$Shortcut.TargetPath       = $PythonW
$Shortcut.Arguments        = "`"$LauncherPyw`""
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.IconLocation     = $IconPath
$Shortcut.Description      = "Open the Claude Traffic Light HUD launcher"
$Shortcut.Save()

# Stamp the same AppUserModelID that launcher.pyw sets at runtime.
# This makes the pinned taskbar button light up when the launcher is open.
$AppId = "Claude.TrafficLight.Launcher"
try {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class LnkAppId {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PROPERTYKEY { public Guid fmtid; public uint pid; }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        int GetCount(out uint c);
        int GetAt(uint i, out PROPERTYKEY k);
        int GetValue(ref PROPERTYKEY k, out PropVariant v);
        int SetValue(ref PROPERTYKEY k, ref PropVariant v);
        int Commit();
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr pszVal;
        public static PropVariant FromString(string s) {
            var pv = new PropVariant { vt = 31 };
            pv.pszVal = Marshal.StringToCoTaskMemUni(s);
            return pv;
        }
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern int SHGetPropertyStoreFromParsingName(
        string path, IntPtr pbc, uint flags, ref Guid riid, out IPropertyStore ppv);

    public static void SetAppId(string lnkPath, string appId) {
        var key = new PROPERTYKEY {
            fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5
        };
        var iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        int hr = SHGetPropertyStoreFromParsingName(lnkPath, IntPtr.Zero, 2, ref iid, out store);
        if (hr != 0) throw new Exception("HRESULT 0x" + hr.ToString("X8"));
        var pv = PropVariant.FromString(appId);
        store.SetValue(ref key, ref pv);
        store.Commit();
        Marshal.ReleaseComObject(store);
        Marshal.FreeCoTaskMem(pv.pszVal);
    }
}
'@ -ErrorAction Stop
    [LnkAppId]::SetAppId($ShortcutDst, $AppId)
    Write-Host "  AppUserModelID: $AppId" -ForegroundColor DarkGray
} catch {
    Write-Warning "Could not stamp AppUserModelID: $_"
    Write-Warning "Taskbar grouping may be imperfect — re-run as admin if this persists."
}

Write-Host ""
Write-Host "Shortcut created:" -ForegroundColor Green
Write-Host "  $ShortcutDst" -ForegroundColor Cyan
Write-Host ""
Write-Host "To pin it to the taskbar:" -ForegroundColor Yellow
Write-Host "  1. Right-click 'Claude Traffic Light' on your Desktop"
Write-Host "  2. Select 'Pin to taskbar'"
Write-Host "  (If already pinned, unpin first then re-pin so the new AppID takes effect)"
Write-Host ""
