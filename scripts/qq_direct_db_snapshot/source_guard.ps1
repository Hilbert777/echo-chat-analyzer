# Private stdin/stdout protocol. Never write source paths or native exceptions.
$ErrorActionPreference = 'Stop'
try {
    [Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    $request = [Console]::ReadLine() | ConvertFrom-Json
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.IO.MemoryMappedFiles;
using System.Runtime.InteropServices;
using System.Diagnostics;
using Microsoft.Win32.SafeHandles;
public static class EchoSourceGuard {
    [StructLayout(LayoutKind.Sequential)] struct Overlap {
        public UIntPtr Internal, InternalHigh;
        public uint Offset, OffsetHigh;
        public IntPtr Event;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool LockFileEx(SafeFileHandle file, uint flags, uint reserved,
        uint length, uint high, ref Overlap overlap);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool UnlockFileEx(SafeFileHandle file, uint reserved,
        uint length, uint high, ref Overlap overlap);
    sealed class Held : IDisposable {
        SafeFileHandle file; uint length; Overlap overlap;
        public Held(FileStream stream, uint offset, uint count) {
            file=stream.SafeFileHandle; length=count; overlap.Offset=offset;
            // Shared + FAIL_IMMEDIATELY. Never wait while holding a subset.
            if(!LockFileEx(file,1,0,length,0,ref overlap)) throw new IOException();
        }
        public void Dispose() {
            if(file!=null) {
                bool ok=UnlockFileEx(file,0,length,0,ref overlap); file=null;
                if(!ok) throw new IOException();
            }
        }
    }
    static FileStream Open(string path) {
        // Deny deletion/replacement while the selected objects are pinned.
        return new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite);
    }
    public static void Run(string mainPath,string walPath,int budget) {
        if(budget<1 || budget>65000) throw new IOException();
        var clock=Stopwatch.StartNew();
        using(var main=Open(mainPath))
        using(var wal=Open(walPath))
        using(var shm=Open(mainPath+"-shm"))
        // SQLite Windows SHARED main lock, wal-index DMS and checkpoint lock.
        // READ_LOCK(1) pins reuse; CKPT pins Main. This is not a workspace lease
        // or a normal SQLite reader (we do not write aReadMark).
        using(var pending=new Held(main,0x40000000,1))
        using(var shared=new Held(main,0x40000002,510))
        using(var dms=new Held(shm,128,1))
        using(var checkpoint=new Held(shm,121,1))
        using(var reader=new Held(shm,124,1)) {
            pending.Dispose();
            // SQLite updates SHM through a mapping. ReadFile coherence with
            // mapped writes is not guaranteed by Windows; use a mapped view.
            using(var map=MemoryMappedFile.CreateFromFile(shm,null,0,
                MemoryMappedFileAccess.Read,HandleInheritability.None,true))
            using(var view=map.CreateViewAccessor(0,136,MemoryMappedFileAccess.Read)) {
                Console.WriteLine("ready"); Console.Out.Flush();
                while(true) {
                    int remaining=budget-(int)clock.ElapsedMilliseconds;
                    if(remaining<=0) throw new IOException();
                    // .NET Framework's synchronized Console reader may run
                    // ReadLineAsync synchronously. Keep the blocking pipe read
                    // on a background task so the independent TTL really runs.
                    var command=System.Threading.Tasks.Task.Run(()=>Console.In.ReadLine());
                    if(!command.Wait(remaining)) throw new IOException();
                    string line=command.Result;
                    if(line=="release") break;
                    if(line!="witness") throw new IOException(); // Includes stdin EOF.
                    byte[] bytes=new byte[136]; view.ReadArray(0,bytes,0,bytes.Length);
                    Console.WriteLine(Convert.ToBase64String(bytes)); Console.Out.Flush();
                }
            }
        }
        // The acknowledgement follows disposal of every source handle/lock.
        Console.WriteLine("released"); Console.Out.Flush();
    }
}
'@
    [EchoSourceGuard]::Run([string]$request.main, [string]$request.wal, [int]$request.budget)
} catch {
    [Console]::WriteLine('failed')
    exit 1
}
