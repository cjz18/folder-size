using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[ComVisible(true)]
[Guid("7C2E5B1A-4F63-4A0E-9C31-8B6D2F0A91E4")]
[ClassInterface(ClassInterfaceType.None)]
public class FolderSizeCommand : IExplorerCommand
{
    public int GetTitle(IntPtr items, out IntPtr name)
    {
        name = Marshal.StringToCoTaskMemUni("查看占用空间");
        return 0;
    }

    public int GetIcon(IntPtr items, out IntPtr icon)
    {
        string exe;
        string prefix;
        LaunchTarget(out exe, out prefix);
        icon = Marshal.StringToCoTaskMemUni(exe);
        return 0;
    }

    public int GetToolTip(IntPtr items, out IntPtr tip)
    {
        tip = Marshal.StringToCoTaskMemUni("查看这个文件或文件夹占用的磁盘空间");
        return 0;
    }

    public int GetCanonicalName(out Guid guid)
    {
        guid = new Guid("7C2E5B1A-4F63-4A0E-9C31-8B6D2F0A91E4");
        return 0;
    }

    public int GetState(IntPtr items, bool okToBeSlow, out uint state)
    {
        state = 0; // ECS_ENABLED
        return 0;
    }

    public int Invoke(IntPtr items, IntPtr bindCtx)
    {
        try
        {
            string path = FirstPath(items);
            string exe;
            string prefix;
            LaunchTarget(out exe, out prefix);
            var args = "";
            if (!string.IsNullOrEmpty(prefix))
                args = "\"" + prefix + "\"";
            if (!string.IsNullOrEmpty(path))
            {
                if (args.Length > 0)
                    args += " ";
                args += "\"" + path + "\"";
            }
            var start = new ProcessStartInfo();
            start.FileName = exe;
            start.Arguments = args;
            start.UseShellExecute = false;
            Process.Start(start);
            return 0;
        }
        catch (Exception ex)
        {
            try
            {
                File.WriteAllText(Path.Combine(Path.GetTempPath(), "foldersize-menu.log"), ex.ToString());
            }
            catch
            {
            }
            return unchecked((int)0x80004005);
        }
    }

    public int GetFlags(out uint flags)
    {
        flags = 0;
        return 0;
    }

    public int EnumSubCommands(out IntPtr enumerator)
    {
        enumerator = IntPtr.Zero;
        return unchecked((int)0x80004001);
    }

    static void LaunchTarget(out string exe, out string prefix)
    {
        string dir = Path.GetDirectoryName(typeof(FolderSizeCommand).Assembly.Location);
        prefix = "";
        string note = Path.Combine(dir, "target.txt");
        if (File.Exists(note))
        {
            string[] lines = File.ReadAllLines(note);
            if (lines.Length > 0 && File.Exists(lines[0].Trim()))
            {
                exe = lines[0].Trim();
                if (lines.Length > 1)
                    prefix = lines[1].Trim();
                return;
            }
        }
        exe = Path.Combine(dir, "foldersize.exe");
    }

    static string FirstPath(IntPtr items)
    {
        if (items == IntPtr.Zero)
            return "";
        var array = (IShellItemArray)Marshal.GetObjectForIUnknown(items);
        uint count;
        array.GetCount(out count);
        if (count == 0)
            return "";
        IShellItem item;
        array.GetItemAt(0, out item);
        IntPtr psz;
        item.GetDisplayName(0x80058000, out psz);
        try
        {
            return Marshal.PtrToStringUni(psz) ?? "";
        }
        finally
        {
            if (psz != IntPtr.Zero)
                Marshal.FreeCoTaskMem(psz);
        }
    }
}

internal static class Program
{
    [STAThread]
    static void Main()
    {
        var reg = new RegistrationServices();
        reg.RegisterTypeForComClients(
            typeof(FolderSizeCommand),
            RegistrationClassContext.LocalServer,
            RegistrationConnectionType.MultipleUse);
        Application.Run();
    }
}

[ComImport]
[Guid("A08CE4D0-FA25-44AB-B57C-C7B1C323E0B9")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IExplorerCommand
{
    [PreserveSig]
    int GetTitle(IntPtr items, out IntPtr name);
    [PreserveSig]
    int GetIcon(IntPtr items, out IntPtr icon);
    [PreserveSig]
    int GetToolTip(IntPtr items, out IntPtr tip);
    [PreserveSig]
    int GetCanonicalName(out Guid guid);
    [PreserveSig]
    int GetState(IntPtr items, [MarshalAs(UnmanagedType.Bool)] bool okToBeSlow, out uint state);
    [PreserveSig]
    int Invoke(IntPtr items, IntPtr bindCtx);
    [PreserveSig]
    int GetFlags(out uint flags);
    [PreserveSig]
    int EnumSubCommands(out IntPtr enumerator);
}

[ComImport]
[Guid("B63EA76D-1F85-456F-A19C-48159EFA858B")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellItemArray
{
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetPropertyStore(int flags, ref Guid riid, out IntPtr ppv);
    void GetPropertyDescriptionList(ref Guid keyType, ref Guid riid, out IntPtr ppv);
    void GetAttributes(int attribFlags, uint mask, out uint attribs);
    void GetCount(out uint count);
    void GetItemAt(uint index, out IShellItem item);
    void EnumItems(out IntPtr enumerator);
}

[ComImport]
[Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IShellItem
{
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out IShellItem parent);
    void GetDisplayName(uint sigdnName, out IntPtr name);
    void GetAttributes(uint mask, out uint attribs);
    void Compare(IShellItem other, uint hint, out int order);
}
