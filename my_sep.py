from flask import Flask , render_template, request , session , jsonify , send_file , abort
import secrets
import yt_dlp
import os
import ffmpeg
from pathlib import Path
import shutil
import uuid
import torch
from openunmix.predict import separate
import soundfile as sf
from threading import Thread
import time
from datetime import datetime, timedelta
from scipy.signal import butter, lfilter
import noisereduce as nr

DOWNLOAD_FOLDER = Path(__file__).parent / "download_pool"
MAX_AGE = timedelta(hours=1)            
CHECK_INTERVAL = 15 * 60

def cleanup_old_files():
    while True:
        now = datetime.now()
        for file in DOWNLOAD_FOLDER.iterdir():
            if file.is_file():
                file_mtime = datetime.fromtimestamp(file.stat().st_mtime)
                if now - file_mtime > MAX_AGE:
                    try:
                        file.unlink()
                        print(f"Deleted old file: {file}")
                    except Exception as e:
                        print(f"Failed to delete {file}: {e}")
        time.sleep(CHECK_INTERVAL)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
ADMIN_PASSWORD = "UnitedNations2005"

cleanup_thread = Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()

def clean_up(user_localname: str):
    downloads_dir = Path("download_pool")
    if not downloads_dir.exists():
        return  

    for file in downloads_dir.glob(f"{user_localname}*"):
        if file.is_file():
            file.unlink()

def ensure_wav(audio_path):
    base, ext = os.path.splitext(audio_path)

  
    if ext.lower() == ".wav":
        return audio_path  

    wav_path = base + ".wav"

    (
        ffmpeg
        .input(audio_path)
        .output(wav_path, ar=44100, ac=2)
        .overwrite_output()
        .run()
    )

    # Delete the original (non-wav)
    if os.path.exists(audio_path):
        os.remove(audio_path)

    return wav_path

def highpass_filter(y, sr, cutoff=100, order=4):
   
    nyq = 0.5 * sr
    normal_cutoff = cutoff / nyq
    b, a = butter(order, normal_cutoff, btype="high", analog=False)
    return lfilter(b, a, y)

def isolate_vocals(input_wav, output_dir="download_pool"):
  
    input_path = Path(input_wav).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

   
    unique_id = str(uuid.uuid4())[:8]
    temp_folder = output_dir / f"temp_{unique_id}"
    temp_folder.mkdir(parents=True, exist_ok=True)

    base_name = input_path.stem
    vocals_path = temp_folder / "vocals.wav"

    waveform, sr = sf.read(str(input_path))
    waveform = torch.tensor(waveform.T, dtype=torch.float32).unsqueeze(0)  # (1, channels, samples)

 
    estimates = separate(
        waveform,
        targets=["vocals"],
        rate=sr,
        device="cpu",
        residual=True,
        niter=4
    )

    vocals_np = estimates["vocals"].squeeze(0).T
    vocals_np = highpass_filter(vocals_np, sr, cutoff=90)
   
    sf.write(str(vocals_path), vocals_np, sr)

    input_path.unlink()

    final_vocals_path = output_dir / f"{base_name}.wav"
    shutil.move(str(vocals_path), str(final_vocals_path))

  
    shutil.rmtree(temp_folder, ignore_errors=True)

    return str(final_vocals_path)

def merge_vocals_with_video(video_path, vocals_wav, user_localname, out_dir="download_pool"):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    
    temp_folder = out_dir / f"temp_{uuid.uuid4().hex[:8]}"
    temp_folder.mkdir(parents=True, exist_ok=True)

    
    temp_aac = temp_folder / "vocals.aac"
    ffmpeg.input(str(vocals_wav)).output(str(temp_aac), acodec="aac", ac=2, ar=44100).run(overwrite_output=True)

    
    temp_merged = temp_folder / "merged.mp4"
    video_in = ffmpeg.input(str(video_path))
    audio_in = ffmpeg.input(str(temp_aac))

    stream = ffmpeg.output(video_in, audio_in, str(temp_merged),
                           vcodec="copy", acodec="copy")
    ffmpeg.run(stream, overwrite_output=True)

   
    for f in [video_path, vocals_wav]:
        try:
            Path(f).unlink()
        except FileNotFoundError:
            pass

   
    final_path = out_dir / f"{user_localname}.mp4"
    shutil.move(str(temp_merged), str(final_path))

   
    shutil.rmtree(temp_folder, ignore_errors=True)

    return str(final_path)

@app.route('/')
def index():
    if 'user_id' not in session:
        session['user_id'] = secrets.token_hex(16)
    return render_template('index.html')

@app.route("/process", methods=["POST"])
def process():
     

    user_key = session.get('user_id')
    if not user_key:
        return "Session expired. Please refresh the page.", 400
    
    session["done"] = False
    session["file_path"] = None

    user_localname = session['user_id']
    clean_up(user_localname)
    
    url = request.form.get("url")

    if not url:
        return "No URL provided", 400
    
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

     

    if url:
        with open("adminonly/url_register", "a") as f:
            f.write(url + "\n")

    ydl_opts = {
    "quiet": True,
    "skip_download": True,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
    "retries": 10,
    "socket_timeout": 30,
    "geo_bypass": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            video_codec = info.get("vcodec")
            audio_codec = info.get("acodec")
            formats = info.get("formats", [])


            if audio_codec != "none" and video_codec != "none":
                output_path = None
                final_file = None

                video_format = next(
                    (f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none" and 480 <= f.get("height", 0) <= 720),
                    None
                    )
                if video_format:
                    ext = video_format.get("ext", "mp4")
                    output_path = Path("download_pool") / f"{user_localname}.{ext}"

                    ydl_opts = {
                    "quiet": True,
                    "format": video_format["format_id"],
                    "outtmpl": str(output_path),
                    "noplaylist": True,
                    "retries": 10
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])


                audio_format = next(
                    (f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"),
                    None
                    )
                if audio_format:
                    ext = audio_format.get("ext", "mp3")
                    output_path_audio = Path("download_pool") / f"{user_localname}.{ext}"

                    ydl_opts = {
                        "quiet": True,
                        "format": audio_format["format_id"],
                        "outtmpl": str(output_path_audio),
                        "noplaylist": True,
                        "retries": 10
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])

                    wav_file = ensure_wav(str(output_path_audio))
                    final_file = isolate_vocals(wav_file)

                if output_path and final_file:
                    merged_file = merge_vocals_with_video(str(output_path), final_file, user_localname)
                    session["file_path"] = merged_file
                    session["done"] = True

                return "video", 200
            
            elif video_codec == "none" and audio_codec != "none":
                audio_format = next(
                    (f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"),
                    None
                    )
                if audio_format:
                    ext = audio_format.get("ext", "mp3")
                    output_path_audio = Path("download_pool") / f"{user_localname}.{ext}"

                    ydl_opts = {
                        "quiet": True,
                        "format": audio_format["format_id"],
                        "outtmpl": str(output_path_audio),
                        "noplaylist": True,
                        "retries": 10
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])

                    wav_file = ensure_wav(str(output_path_audio))
                    final_file = isolate_vocals(wav_file)

                    if final_file:
                        session["file_path"] = final_file
                        session["done"] = True

                return "audio", 200
            
            else:
                return "No audio or video detected", 400

        except yt_dlp.utils.DownloadError as e:
            return f"Error processing URL: {str(e)}", 400

    return "OK", 200

@app.route("/status", methods=["GET"])
def status():

    user_key = session.get('user_id')
    if not user_key:
        return "Session expired. Please refresh the page.", 400

    if session.get("done" , False) and session.get("file_path"):
        return jsonify({"ready": True})
  
    return jsonify({"status": "processing"})

@app.route("/download", methods=["GET"])
def download():
    user_key = session.get('user_id')
    if not user_key:
        return "Session expired. Please refresh the page.", 400

    file_path = session.get("file_path")
    if not file_path:
        return abort(404, "File not found or session expired.")

    file_path = Path(file_path)
    if not file_path.exists():
        return abort(404, "File not found on server.")

    session["file_path"] = None
    session["done"] = False

    return send_file(
        file_path,
        as_attachment=True,
        download_name=file_path.name
    )

@app.route("/admin" , methods=["GET" , "POST"])
def admin():
    user_key = session.get('user_id')
    if not user_key:
        return "Session expired. Please refresh the page.", 400

    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            try:
                with open("adminonly/url_register", "r") as f:
                    urls = f.readlines()  
            except FileNotFoundError:
                return "No URLs found."

            return "<br>".join([url.strip() for url in urls])
        else:
            return "Wrong password", 403

    return '''
        <form method="POST">
            <input type="password" name="password" placeholder="Enter admin password">
            <button type="submit">Login</button>
        </form>

    '''

if __name__ == '__main__':
    app.run(debug=True , use_reloader=True)
