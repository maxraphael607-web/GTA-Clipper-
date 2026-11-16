import collections
import glob
import os
import socket
import subprocess
import sys
import threading
import time

if len(sys.argv) < 2:
    sys.exit("Usage: python clipper.py CHANNEL_NAME")
CHANNEL = sys.argv[1].lower().lstrip("#")

# ---- Settings you can tune ----
QUALITY = "720p60,720p,best"   # fallback order
SEGMENT_SECONDS = 10           # length of each buffered piece
WINDOW_SECONDS = 10            # chat counting window
HISTORY_WINDOWS = 30           # windows used for the rolling average
MIN_WINDOWS = 6                # wait for this many windows before triggering
SPIKE_MULTIPLIER = 3.0         # spike = this many times the average
MIN_MESSAGES = 10              # ignore tiny spikes in quiet chat
COOLDOWN_SECONDS = 60          # minimum gap between clips
AFTERMATH_SECONDS = 10         # keep recording this long after a spike
CLIP_SEGMENTS = 6              # 6 x 10s = about 60s per clip
MAX_RUNTIME = 5.5 * 3600       # stop before GitHub's 6-hour limit
# --------------------------------

os.makedirs("buffer", exist_ok=True)
os.makedirs("clips", exist_ok=True)

stop_event = threading.Event()
lock = threading.Lock()
msg_count = 0


def chat_listener():
    """Read Twitch chat anonymously and count messages."""
    global msg_count
    while not stop_event.is_set():
        try:
            s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
            s.sendall(b"PASS SCHMOOPIIE\r\nNICK justinfan12345\r\n")
            s.sendall(f"JOIN #{CHANNEL}\r\n".encode())
            buf = ""
            while not stop_event.is_set():
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data.decode("utf-8", errors="ignore")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line.startswith("PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG " in line:
                        with lock:
                            msg_count += 1
        except OSError as e:
            print("Chat connection issue:", e)
        time.sleep(5)


def segments():
    return sorted(glob.glob("buffer/seg_*.ts"))


def cleanup():
    """Keep only the newest few minutes of video."""
    for f in segments()[: -(CLIP_SEGMENTS * 3)]:
        try:
            os.remove(f)
        except OSError:
            pass


def make_clip():
    print("Spike detected, recording aftermath...")
    time.sleep(AFTERMATH_SECONDS)
    segs = segments()[:-1][-CLIP_SEGMENTS:]  # skip the piece still being written
    if not segs:
        print("No segments available yet.")
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    list_path = f"buffer/list_{stamp}.txt"
    with open(list_path, "w") as f:
        for seg in segs:
            f.write(f"file '{os.path.abspath(seg)}'\n")
    out = f"clips/clip_{CHANNEL}_{stamp}.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", list_path, "-c", "copy", "-bsf:a", "aac_adtstoasc", out]
    )
    os.remove(list_path)
    print("Saved", out)


# ---- Start recording ----
streamlink = subprocess.Popen(
    ["streamlink", f"https://twitch.tv/{CHANNEL}", QUALITY, "-O"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
ffmpeg = subprocess.Popen(
    ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
     "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
     "-segment_format", "mpegts", "-strftime", "1",
     "-reset_timestamps", "1", "buffer/seg_%Y%m%d_%H%M%S.ts"],
    stdin=streamlink.stdout,
)
streamlink.stdout.close()

threading.Thread(target=chat_listener, daemon=True).start()
print(f"Watching {CHANNEL}...")

history = collections.deque(maxlen=HISTORY_WINDOWS)
last_clip = 0
start = time.time()

while time.time() - start < MAX_RUNTIME:
    time.sleep(WINDOW_SECONDS)
    if ffmpeg.poll() is not None:
        print("Stream ended or is offline. Stopping.")
        break

    with lock:
        count = msg_count
        msg_count = 0

    avg = sum(history) /
