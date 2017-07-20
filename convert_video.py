#!/usr/bin/python

import os
import re
import subprocess
import sys
import time


# To make a thumbnail from the first frame, issue a command like:
#    ffmpeg.exe -i (video file) -frames:v 1 (output).jpg

def duration_format(dur, digits=0):
    """Format a duration in seconds as [h:m]m:ss[.sss].
    Enter: dur: duration in seconds.
             digits: number of decimal digits to show in the seconds."""
    intdur = int(dur)
    if dur<3600:
        durstr = "%d:%02d"%(intdur/60, intdur%60)
    else:
        durstr = "%d:%02d:%02d"%(intdur/3600, (intdur/60)%60, intdur%60)
    if digits:
        durstr += ".%*d"%(digits, (dur-intdur)*(10**digits))
    return durstr


def duration_parse(durstr):
    """Parse a duration of the form [[hh:]mm:]ss[.sss] and return a float
     with the duration or None for failure to parse.
    Enter: durstr: string with the duration.
    Exit:  duration: duration in seconds."""
    try:
        durstr = durstr.strip().split(":")
        dur = 0
        for part in xrange(len(durstr)):
            dur += float(durstr[-1-part])*(60**part)
    except Exception:
        return None
    return dur


def make_status(starttime, timedone, origdur=None, final=False):
    """Create a status string.
    Enter: starttime: epoch this process started.
           timedone: amount of work that has been done.  0 or None for
                     unknown.
           origdur: expected total work.  0 or None for unknown.
           final: if True, this is no longer an estimate.
    Exit:  status: a status string."""
    curtime = time.time()
    total = curtime-starttime
    status = ""
    if timedone and total:
        if final:
            status += "Final"
        else:
            status += "Estimated"
        status += " %4.2fs"%timedone
        if origdur:
            status += "/%4.2fs"%origdur
        try:
            curlen = os.stat(opts["files"][1]).st_size
            status += " (%d bytes"%curlen
            if origdur:
                if timedone:
                    speed = timedone/total
                else:
                    speed = origdur/total
                if speed>=100:
                    speed = 99.99
                status += ", %4.2fx"%speed
            status += ")"
        except Exception:
            pass
        if origdur and timedone<origdur and not final:
            left = (origdur-timedone)*total/timedone
            status += " %s left"%duration_format(left)
            total += left
        status += " "
    status += "%s total"%duration_format(total)
    return status


if __name__=='__main__':
    ProgramPath = os.path.abspath(sys.argv[0])
    ProgramRoot = os.path.split(ProgramPath)[0]
    Help = False
    Verbose = 0
    done = False
    opts = {"files":[], "factor":"1.5"}
    for arg in sys.argv[1:]:
        if arg=='--help':
            Help = True
        elif arg=="--":
            done = True
        elif arg.startswith("--") and not done:
            if "=" in arg:
                opts[arg[2:].split("=", 1)[0]] = arg.split("=", 1)[1]
            elif arg.startswith("--no"):
                opts[arg[4:]] = False
            else:
                opts[arg[2:]] = True
        elif arg.startswith("-") and not done:
            for k in arg[1:]:
                if k=="v":
                    Verbose += 1
                else:
                    Help = True
        else:
            opts["files"].append(arg)
    if len(opts["files"])<1 or len(opts["files"])>2:
        Help = True
    if Help:
        print """Convert a video using ffmpeg and some preferred settings.

Syntax: convert.py (input file) [(output file)] -v --format=(format)
        --duration=(seconds) --start=(seconds) --end=(seconds) --ifps=(fps)
        --seq=(start number) --quiet --maxwidth=(width) --width=(width)
        --faster=(factor) --rotate=(90|-90|180) --ofps=(fps) --delay=(seconds)
        --kbps=(rate) --factor=(rate factor) --outdir=(path) --autolevels
        --crop[=w:h:x:y]

The input file can be any ffmpeg-readable video format.  The output file will
be an mp4 unless otherwise specified.  If the output file is a directory, then
the original filename with a modified extension is placed in that directory
(this is the same as not specifying an output file and specifying --outdir).
--autolevels adds automatic brightness and contrast correction.
--crop examines the central 8 seconds of video and then crops the video based
 on that.  If values are specified, those are used.
--delay specifies a delay between loops for gif animations.
--duration limits the output to that number of seconds.  It can also be
 specified as [[hh:]mm:]ss.sss.  If this is specified, --end is ignored.  This
 is the value before the faster value is applied.
--end stops converting video at the specified time in seconds.  If --duration
 is specified, this is ignored.  It can also be specified as [[hh:]mm:]ss.sss.
 This is the value before the faster value is applied.
--faster speeds up the output video by the specified amount.
--factor limits the output rate compared to the original.  Default is 1.5.  0
 for no limit.
--format overrides the format detected from the output file extension.
--ifps specifies the input frames per second (useful for sequences or broken
 files).
--kbps specifies the output rate of video.  If not specified, this is limited
 based on the width and the factor.
--maxwidth specifies the new maximum width of the output.  Unless overridden,
 this also determines the output bitrate.
--outdir places the output file in the specified path.
--ofps specifies the output frames per second (useful for gif output, for
 instance).
--quiet reduces the volume to essentially zero.
--rotate rotates the specified number of degrees clockwise.
--seq uses a sequence of images.  This specifies the numerical portion of the
 first image.  The input file must include a %d parameter to determine how a
 sequence will be found (e.g., DSC_%04.jpg).  Images will be read until one is
 missing.
--start skips that number of seconds at the beginning of the input video.  It
 can also be specified as [[hh:]mm:]ss.sss.  This is the time before the
 faster value is applied.
--width specifies the new width of the output.  Unless overridden, this also
 determines the output bitrate.
-v increases verbosity."""
        sys.exit(0)
    preptime = time.time()
    if len(opts["files"])>1:
        if os.path.isdir(opts["files"][1]):
            opts["outdir"] = opts["files"][1]
            opts["files"] = opts["files"][:1]
    cmd = [os.path.join(ProgramRoot, "ffmpeg")]
    cmd.extend(["-i", opts["files"][0]])
    if Verbose>=3:
        print " ".join(cmd)
    info = subprocess.Popen(cmd, stderr=subprocess.PIPE).stderr.read()
    info = info.replace("\r", "\n").split("\n")
    origw = origh = origfps = origkbps = None
    origdur = None
    origakbps = orighz = None
    for line in info:
        if re.match("^\s+Stream #.+?: Video", line) and not origw:
            for part in line.split(",")[1:]:
                if "x" in part and not origw:
                    origw = int(part.split("x")[0].split()[-1].strip())
                    origh = int(part.strip().split("x")[1].split()[0].strip())
                elif " fps" in part and not origfps:
                    origfps = float(part.split(" fps")[0].strip())
                elif " kb/s" in part and not origkbps:
                    origkbps = float(part.split(" kb/s")[0].strip())
        if re.match("^\s+Stream #.+?: Audio", line) and not origakbps:
            for part in line.split(",")[1:]:
                if " kb/s" in part and not origakbps:
                    origakbps = float(part.split(" kb/s")[0].strip())
                elif " Hz" in part and not orighz:
                    orighz = float(part.split(" Hz")[0].strip())
        if re.match("^\s+Duration: ", line) and not origdur:
            origdur = duration_parse(line.split("Duration:", 1)[1].split(",")[0])
    if Verbose>=2:
        if origw:
            print("Info width and height: %d x %d"%(origw, origh))
        if origfps:
            print("Info frame rate: %4.2f"%origfps)
        if origkbps:
            print("Info video bit rate: %d kbps"%origkbps)
        if origakbps:
            print("Info audio bit rate: %d kbps"%origakbps)
        if orighz:
            print("Info audio sample rate: %d Hz"%orighz)
        if origdur:
            print("Info duration: %4.2fs"%origdur)
    if "format" in opts:
        format = opts["format"]
    elif len(opts["files"])==2:
        format = opts["files"][1].rsplit(".", 1)[1]
    else:
        format = "mp4"
    if len(opts["files"])==1:
        opts["files"].append(opts["files"][0].rsplit(".", 1)[0]+".out.%s"%format)
    if "outdir" in opts:
        opts["files"][1] = os.path.join(opts["outdir"], os.path.split(opts["files"][1])[1])

    # Get real frame count and duration
    if not "seq" in opts:
        cmd = [os.path.join(ProgramRoot, "ffmpeg")]
        cmd.extend(["-i", opts["files"][0]])
        cmd.extend(["-vcodec","copy","-an","-f","null","NUL"])
        if Verbose>=3:
            print " ".join(cmd)
        info = subprocess.Popen(cmd, stderr=subprocess.PIPE).stderr.read()
        info = info.replace("\r", "\n").split("\n")
        calcdur = None
        for line in info:
            if line.startswith("frame=") and " time=" in line:
                calcframe = int(line.split("frame=")[1].split()[0])
                calcdur = duration_parse(line.split(" time=")[1].split()[0])
        if calcdur:
            origdur = calcdur
            origframes = calcframe
            if Verbose>=2:
                print("Scan duration: %4.2fs"%origdur)
                print("Scan frames: %d"%origframes)
                print("Scan frame rate: %4.2f"%(float(origframes)/origdur))

    crop = None
    if opts.get("crop", False) not in (False, True):
        crop = "crop=" + opts.get("crop")
    if opts.get("crop", False) is True and origdur and not "seq" in opts:
        cmd = [os.path.join(ProgramRoot, "ffmpeg")]
        cmd.extend(["-i", opts["files"][0]])
        cropstart = int((origdur-8)/2)
        if cropstart<0:
            cropstart = 0
        cmd.extend(["-vf","cropdetect","-ss","%d"%cropstart,"-t","8","-f","null","NUL"])
        if Verbose>=3:
            print " ".join(cmd)
        info = subprocess.Popen(cmd, stderr=subprocess.PIPE).stderr.read()
        info = info.replace("\r", "\n").split("\n")
        for line in info:
            if "cropdetect" in line and " crop=" in line:
                crop = "crop="+line.split(" crop=", 1)[1].split()[0]
                parts = crop.split("=", 1)[1].split(":")
                origw = int(parts[0])
                origh = int(parts[1])
        if crop:
            if Verbose>=2:
                print("Crop: %s"%crop)
    vf = []
    af = []
    cmd = [os.path.join(ProgramRoot, "ffmpeg")]
    cmd.append("-y")
    if "ifps" in opts:
        cmd.extend(["-r", opts["ifps"]])
    if "seq" in opts:
        cmd.extend(["-start_number", opts["seq"]])
        # Since there is no audio in a sequence of images, generate silence to
        # match the length of the sequence
        cmd.extend(["-f","lavfi","-i","aevalsrc=0","-shortest"])
    elif origw and not origakbps:
        cmd.extend(["-f","lavfi","-i","aevalsrc=0"])
    cmd.extend(["-i", opts["files"][0]])
    if not "seq" in opts:
        factor = 1
        if "faster" in opts:
            factor = float(opts["faster"])
        if "start" in opts:
            cmd.extend(["-ss", "%5.3f" % (duration_parse(opts["start"])/factor)])
        if "duration" in opts:
            cmd.extend(["-t", "%5.3f" % (duration_parse(opts["duration"])/factor)])
        elif "end" in opts:
            cmd.extend(["-t", "%5.3f" % ((duration_parse(opts["end"])-duration_parse(opts.get("start", 0)))/factor)])
    w = None
    kbps = None
    audio_kbps = None
    if "maxwidth" in opts and origw and origh:
        w = int(opts["maxwidth"])
        if opts.get("rotate", "") in ("90", "-90"):
            if w>origh:
              w = origh
        else:
            if w>origw:
              w = origw
        if not "width" in opts:
            opts["width"] = "%d"%w
    if "width" in opts:
        w = int(opts["width"])
        if w<=720:
            kbps = int((w/720)**0.5*6448)
        elif w<=1280:
            kbps = int((w/1280)**0.85*10800)
        elif w<=1920:
            kbps = int((w/1920)*16384)
        else:
            kbps = int((w/1920)*16384)
        if origkbps and float(opts.get("factor", 0)):
            if kbps>origkbps*float(opts["factor"]):
                kbps = None
        if not "kbps" in opts and kbps:
            opts["kbps"] = kbps
    if "kbps" in opts:
        kbps = int(opts["kbps"])
    if not kbps and origkbps and float(opts.get("factor", 0)):
        kbps = origkbps*float(opts.get("factor", 0))
    maxkbps = 50000
    bufsize = 62500
    h264level = 41
    if w and kbps:
        if kbps<=7000 and w<=720:
            maxkbps = 10000
            bufsize = 10000
            h264level = 30
        elif kbps<=10000 and w<=1280:
            maxkbps = 14000
            bufsize = 14000
            h264level = 31
        elif kbps<=15000 and w<=1280:
            maxkbps = 20000
            bufsize = 20000
            h264level = 32
        else:
            maxkbps = 50000
            bufsize = 62500
            h264level = 41
    akbps = None
    if kbps:
        if "quiet" in opts:
            maxakbps = 64
        elif kbps>8192:
            maxakbps = 448
        elif kbps>4096:
            maxakbps = 288 # Max per channel; we don't know how many channels
        elif kbps>1200:
            maxakbps = 192
        else:
            maxakbps = 128
    if "akbps" in opts:
        akbps = int(opts["akbps"])
    if not akbps and origakbps and float(opts.get("factor", 0)):
        akbps = origakbps*float(opts.get("factor", 0))
    if (akbps and akbps>maxakbps) or "quiet" in opts:
        akbps = maxakbps
    cmd.extend(["-map_metadata","-1"])
    cmd.extend(["-f",format])
    if format=="mp4":
        cmd.extend(["-vcodec","libx264"])
        cmd.extend(["-flags","+loop","-subq","4","-trellis","0","-refs","1","-coder","0","-me_method","hex","-me_range","16","-keyint_min","15","-sc_threshold","40","-i_qfactor","0.71","-bt","200k","-rc_eq","blurCplx^^^(1-qComp^)","-qcomp","0.6","-qmin","10","-qmax","51","-qdiff","2","-g","15","-async","2","-bf","0","-deblock","2:2"])
        cmd.extend(["-maxrate","%dk"%maxkbps])
        cmd.extend(["-bufsize","%dk"%bufsize])
        cmd.extend(["-level","%d"%h264level])
        cmd.extend(["-pix_fmt","yuv420p"])
        cmd.extend(["-movflags","faststart"])
    if format=="gif":
        cmd.extend(["-pix_fmt","pal8"])
        if "delay" in opts:
            cmd.extend(["-final_delay", "%d"%int(float(opts["delay"])*100)])
    if kbps:
        cmd.extend(["-vb","%sk"%kbps])
    if crop:
        vf.append(crop)
    if "rotate" in opts:
        if opts["rotate"]=="90":
            vf.append("transpose=1")
        elif opts["rotate"]=="-90":
            vf.append("transpose=2")
        elif opts["rotate"] in ("180","-180"):
            vf.extend(["vflip", "hflip"])
    if opts.get("autolevels", False):
        vf.append("pp=al")
    if w:
        vf.append("scale=%d:-2"%w)
    if "faster" in opts:
        vf.append("setpts=(1/%s)*PTS"%opts["faster"])
        factor = float(opts["faster"])
        if factor>1:
            while factor>2:
                af.append("atempo=2.0")
                factor /= 2
        if factor<1:
            while factor<0.5:
                af.append("atempo=0.5")
                factor *= 2
        if factor!=1:
            af.append("atempo=%5.3f" % factor)
    if len(vf):
        cmd.extend(["-vf", ",".join(vf)])
    cmd.append("-shortest")
    if format=="mp4":
        cmd.extend(["-strict","experimental","-acodec","aac","-aq","5"])
        if orighz and not orighz in [44100, 48000]:
            cmd.extend(["-ar","48000"])
    else:
        cmd.extend(["-acodec","libmp3lame","-aq","5"])
        if orighz and not orighz in [44100, 48000]:
            cmd.extend(["-ar","44100"])
    if akbps:
        cmd.extend(["-ab","%sk"%akbps])
    if "quiet" in opts:
        af.append("volume=-90dB")
        cmd.extend(["-ac","1"])
    else:
        cmd.extend(["-ac","2"])
    if len(af):
        cmd.extend(["-af", ",".join(af)])
    if "ofps" in opts:
        cmd.extend(["-r", opts["ofps"]])
    cmd.append(opts["files"][1])
    if Verbose>=3:
        print " ".join(cmd)

    timedone = None
    starttime = time.time()
    pptr = subprocess.Popen(cmd, stderr=subprocess.PIPE).stderr
    data = ""
    while True:
        data += pptr.read(80)
        if not data:
            break
        if not "\n" in data and not "\r" in data:
            continue
        lines = data.replace("\r", "\n").split("\n")
        line = lines[-2]
        data = lines[-1]
        if Verbose>=4:
            for part in lines[:-1]:
                if part.rstrip():
                    print part.rstrip()
        if Verbose>=1:
            if line.startswith("frame=") and " time=" in line:
                timedone = duration_parse(line.split(" time=")[1].split()[0])
                status = make_status(starttime, timedone, origdur)
                if Verbose<=2:
                    sys.stderr.write("\r%-79s\r"%status[:79])
                else:
                    sys.stderr.write("%-79s\n"%status[:79])
                sys.stderr.flush()
    pptr.close()
    if Verbose>=1:
        status = make_status(preptime, timedone, origdur, final=True)
        sys.stderr.write("%-79s\n"%status[:79])
        sys.stderr.flush()
