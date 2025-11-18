from flask import Flask, Response, render_template, request, redirect, url_for, send_from_directory, jsonify, send_file
from werkzeug.utils import secure_filename
from PIL import Image
from PIL import ExifTags
from pathlib import Path
import os
import json
import subprocess
import random
import time
import logging
import threading
import requests

config_lock = threading.Lock()
app = Flask(__name__)
script_dir = "/home/pi/Rolex"
allowed_scripts = {
    "RolexWOOD.py",
    "RolexCRAZY.py",
    "RolexGOLD.py",
    "PalmPilot.py",
    "Rolex1908.py",
    "RolexBLUE.py",
    "PlainClock.py",
    "ClockText.py",
    "PatekRotate.py",
    "RolexBLUEDIAMOND.py",
    "Message2.py"
}
clock_process = None
clock_timer = None
active_clock_name = None
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_ROOT = os.path.join(BASE_DIR, 'static', 'uploads')
THUMB_ROOT = os.path.join(BASE_DIR, 'static', 'thumbs')
SYMLINK_PATH = os.path.join(BASE_DIR, 'static', 'current.jpg')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

def create_slideshow_list(folder, images):
    list_path = os.path.join(BASE_DIR, 'slideshow_list.txt')
    try:
        with open(list_path, 'w') as f:
            f.write('\n'.join(images))
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        app.logger.error(f"Failed to create slideshow_list.txt: {e}")

def launch_zoom_viewer():
    subprocess.Popen([
        "feh", "--fullscreen", "--title", "feh-zoom", "/home/pi/frame-app/static/current.jpg"
    ])

def send_zoom(direction="in"):
    key = "Up" if direction == "in" else "Down"
    try:
        win_id = subprocess.check_output([
            "xdotool", "search", "--name", "feh-zoom"
        ]).decode().strip().split('\n')[0]
        subprocess.run(["xdotool", "windowactivate", win_id])
        for _ in range(3):
            subprocess.run(["xdotool", "key", "--window", win_id, key])
        return True
    except:
        return False

def retire_zoom_viewer():
    subprocess.run(["pkill", "-f", "feh.*feh-zoom"])

def update_viewer_state(image_path, reset_delay=False):
    retire_zoom_viewer()  # 👈 Cleanly retire zoom viewer before updating

    try:
        with open(os.path.join(BASE_DIR, 'static', 'current_image.txt'), 'w') as f:
            f.write(os.path.basename(image_path))
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        app.logger.error(f"Failed to update current_image.txt: {e}")

    update_symlink(image_path)
    refresh_viewer()

    if reset_delay:
        Path(os.path.join(BASE_DIR, 'delay_updated.flag')).touch()


# Config
def load_config():
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r') as f:
                config = json.load(f)
                app.logger.info(f"Loaded config: {config}")
                return config
    except Exception as e:
        app.logger.warning(f"Failed to load config: {e}")
    return {'delay': 5, 'current_folder': None}


def save_config(config):
    try:
        temp_path = os.path.join(BASE_DIR, 'config_temp.json')
        final_path = CONFIG_PATH  # already points to config.json

        with open(temp_path, 'w') as f:
            json.dump(config, f)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, final_path)
    except Exception as e:
        app.logger.error(f"Failed to save config atomically: {e}")

def regenerate_image_order(blend_ratio=0.6, cutoff_days=90):
    import time, random, math

    folder_path = os.path.join(UPLOAD_ROOT, 'main')
    if not os.path.exists(folder_path):
        return

    images = [
        f for f in os.listdir(folder_path)
        if not f.startswith('.') and os.path.isfile(os.path.join(folder_path, f))
    ]

    config = load_config()
    weighted = config.get('weighted_shuffle', False)

    if not weighted:
        # 🌱 Non‑weighted: pure random shuffle
        random.seed(time.time())
        random.shuffle(images)
        order = images
    else:
        # Weighted shuffle logic (your existing code)
        now = time.time()
        cutoff_secs = cutoff_days * 86400

        recent_images, archive_images = [], []
        for img in images:
            path = os.path.join(folder_path, img)
            try:
                mtime = os.path.getmtime(path)
                age = now - mtime
                if age <= cutoff_secs:
                    recent_images.append(img)
                else:
                    archive_images.append(img)
            except Exception:
                archive_images.append(img)

        def weight(img):
            try:
                mtime = os.path.getmtime(os.path.join(folder_path, img))
                age = now - mtime
                return math.exp(-age / cutoff_secs)
            except Exception:
                return 0.5

        weighted_recent = [(random.random() * weight(img), img) for img in recent_images]
        weighted_recent.sort(reverse=True)
        recent_order = [img for _, img in weighted_recent]

        archive_order = archive_images[:]
        random.shuffle(archive_order)

        n_recent = int(len(images) * blend_ratio)
        n_archive = len(images) - n_recent
        recent_order = recent_order[:n_recent]
        archive_order = archive_order[:n_archive]

        blended = []
        while recent_order or archive_order:
            if recent_order:
                blended.append(recent_order.pop(0))
            if archive_order:
                blended.append(archive_order.pop(0))

        order = blended

    # Write safely
    order_path = os.path.join(BASE_DIR, 'image_order.txt')
    try:
        with open(order_path, 'w') as f:
            f.write('\n'.join(order))
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        app.logger.error(f"Failed to write image_order.txt: {e}")

# Thumbnail logic
def generate_thumbnail(folder, filename, size=(300, 300), quality=30):
    source_path = os.path.join(UPLOAD_ROOT, folder, filename)
    thumb_folder = os.path.join(THUMB_ROOT, folder)
    os.makedirs(thumb_folder, exist_ok=True)
    thumb_path = os.path.join(thumb_folder, filename)

    try:
        with Image.open(source_path) as img:
            # ✅ EXIF-safe orientation correction
            try:
                if hasattr(img, '_getexif'):
                    exif = img._getexif()
                    if exif:
                        orientation_tag = next(
                            (tag for tag, name in ExifTags.TAGS.items() if name == 'Orientation'), None)
                        orientation = exif.get(orientation_tag)
                        if orientation == 3:
                            img = img.rotate(180, expand=True)
                        elif orientation == 6:
                            img = img.rotate(270, expand=True)
                        elif orientation == 8:
                            img = img.rotate(90, expand=True)
            except Exception as e:
                app.logger.warning(f"EXIF correction failed for {filename}: {e}")

            img.thumbnail(size)  # Resize while preserving aspect ratio
            img.save(thumb_path, quality=quality, optimize=True)
        return thumb_path
    except Exception as e:
        app.logger.warning(f"Thumbnail error for {filename}: {e}")
        return None

@app.route("/clocks")
def choose_clock():
    duration = request.args.get('duration', default='15')
    return render_template("clocks.html", duration=duration)


def stop_active_clock():
    """Terminate any currently running clock or billboard safely."""
    global clock_process, active_clock_name, clock_timer
    if clock_timer:
        clock_timer.cancel()
        clock_timer = None

    if clock_process and clock_process.poll() is None:
        try:
            clock_process.terminate()
            clock_process.wait(timeout=5)
            app.logger.info(f"Stopped '{active_clock_name}'")
        except Exception as e:
            app.logger.error(f"Failed to terminate '{active_clock_name}': {e}")
    clock_process = None
    active_clock_name = None


@app.route('/launch/<script_name>')
def launch(script_name):
    global clock_process, active_clock_name, clock_timer

    if not script_name.endswith(".py"):
        script_name += ".py"
    if script_name not in allowed_scripts:
        return f"Script not allowed: {script_name}", 403

    script_path = os.path.join(script_dir, script_name)
    if not os.path.isfile(script_path):
        return f"Script not found: {script_name}", 404

    try:
        stop_active_clock()  # Kill any existing clock

        env = os.environ.copy()
        env["DISPLAY"] = ":0"
        env["XDG_RUNTIME_DIR"] = "/run/user/1000"

        message = request.args.get('message', '')
        duration = request.args.get('duration', '15')
        try:
            duration_minutes = int(duration)
        except ValueError:
            duration_minutes = 15

        args = ['python3', script_path]
        if message:
            args.append(message)

        clock_process = subprocess.Popen(args, env=env)
        active_clock_name = script_name.replace(".py", "")

        # Schedule termination
        clock_timer = threading.Timer(duration_minutes * 60, stop_active_clock)
        clock_timer.start()

        return redirect(url_for('choose_clock', duration=duration))
    except Exception as e:
        return f"Error launching {script_name}: {e}", 500


@app.route('/launch_billboard')
def launch_billboard():
    global clock_process, active_clock_name, clock_timer

    stop_active_clock()  # Kill any existing clock or billboard

    message = request.args.get('message', '')
    font = request.args.get('font', 'Copperplate')
    size = request.args.get('size', '36')
    color = request.args.get('color', 'black')
    background = request.args.get('background', 'Message_Orange')
    duration = request.args.get('duration', '30')
    try:
        duration_minutes = int(duration)
    except ValueError:
        duration_minutes = 30

    script_path = os.path.join(script_dir, "Message2.py")
    args = ['python3', script_path, message, font, size, color, background, duration]

    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"

    clock_process = subprocess.Popen(args, env=env)
    active_clock_name = "Message2"

    # Schedule termination
    clock_timer = threading.Timer(duration_minutes * 60, stop_active_clock)
    clock_timer.start()

    return redirect(url_for('choose_clock', duration=duration))

@app.route('/billboard')
def billboard():
    duration = request.args.get('duration', '30')
    return render_template('billboard.html', duration=duration)

@app.route("/cancel")
def cancel_clock():
    stop_active_clock()
    return redirect(url_for("index"))

@app.route('/message')
def message():
    duration = request.args.get('duration', '30')  # default to 30 if missing
    return render_template('message.html', duration=duration)


@app.route('/current-full')
def current_full():
    if not os.path.islink(SYMLINK_PATH):
        return 'No current image', 404

    target_path = os.readlink(SYMLINK_PATH)
    full_path = os.path.join(BASE_DIR, target_path) if not os.path.isabs(target_path) else target_path

    if not os.path.exists(full_path):
        return 'Image not found', 404

    return send_file(full_path, mimetype='image/jpeg', cache_timeout=0)

@app.route('/symlink-mtime')
def symlink_mtime():
    try:
        mtime = os.lstat(SYMLINK_PATH).st_mtime
        return jsonify({'mtime': mtime})
    except Exception as e:
        app.logger.warning(f"Failed to read symlink mtime: {e}")
        return jsonify({'mtime': None}), 500


# Symlink logic
def update_symlink(image_path):
    if os.path.exists(SYMLINK_PATH) or os.path.islink(SYMLINK_PATH):
        os.remove(SYMLINK_PATH)
    os.symlink(image_path, SYMLINK_PATH)

    # 🔧 Write real filename for frontend to read
    with open(os.path.join(BASE_DIR, 'static', 'current_filename.txt'), 'w') as f:
        f.write(os.path.basename(image_path))

def get_next_image(folder):
    order_path = os.path.join(BASE_DIR, 'image_order.txt')
    current_path = os.path.join(BASE_DIR, 'static', 'current_filename.txt')

    try:
        with open(order_path, 'r') as f:
            images = f.read().strip().split()

        with open(current_path, 'r') as f:
            current = f.read().strip()

        if current in images:
            idx = images.index(current)
            next_idx = (idx + 1) % len(images)
            return os.path.join(UPLOAD_ROOT, folder, images[next_idx])
        elif images:
            return os.path.join(UPLOAD_ROOT, folder, images[0])
    except Exception as e:
        app.logger.error(f"Failed to get next image: {e}")
        return None

def correct_orientation(img):
    try:
        if hasattr(img, '_getexif'):
            exif = img._getexif()
            if exif:
                orientation_tag = next(
                    (tag for tag, name in ExifTags.TAGS.items() if name == 'Orientation'), None)
                orientation = exif.get(orientation_tag)
                if orientation == 3:
                    img = img.rotate(180, expand=True)
                elif orientation == 6:
                    img = img.rotate(270, expand=True)
                elif orientation == 8:
                    img = img.rotate(90, expand=True)
    except Exception as e:
        app.logger.warning(f"EXIF correction failed: {e}")
    return img

def crop_to_aspect(img, target_ratio=(3, 2)):
    width, height = img.size
    target_w, target_h = target_ratio
    current_ratio = width / height
    desired_ratio = target_w / target_h

    if current_ratio > desired_ratio:
        # Too wide → crop width
        new_width = int(height * desired_ratio)
        left = (width - new_width) // 2
        box = (left, 0, left + new_width, height)
    else:
        # Too tall → crop height
        new_height = int(width / desired_ratio)
        top = (height - new_height) // 2
        box = (0, top, width, top + new_height)

    return img.crop(box)

# Routes
@app.route('/')
def index():
    config = load_config()
    folders = sorted(
        [f for f in os.listdir(UPLOAD_ROOT) if not f.startswith('.')]
    ) if os.path.exists(UPLOAD_ROOT) else []

    current_folder = config.get('current_folder')
    images = []
    if current_folder:
        folder_path = os.path.join(UPLOAD_ROOT, current_folder)
        if os.path.exists(folder_path):
            images = sorted(
                [f for f in os.listdir(folder_path) if not f.startswith('.')]
            )
    raw_delay = config.get('delay', 1200)
    slider_value = raw_delay // 60

    return render_template('index.html', folders=folders, images=images,
                           current_folder=current_folder, delay=slider_value)

@app.route('/set-weighted-shuffle', methods=['POST'])
def set_weighted_shuffle():
    data = request.get_json()
    enabled = data.get('enabled', False)
    config = load_config()
    config['weighted_shuffle'] = enabled
    with config_lock:
        save_config(config)
    return '', 204

@app.route('/zoom_in', methods=['POST'])
def zoom_in():
    if not send_zoom("in"):
        launch_zoom_viewer()
        time.sleep(0.3)
        send_zoom("in")
    return Response(status=204)

@app.route('/zoom_out', methods=['POST'])
def zoom_out():
    if not send_zoom("out"):
        launch_zoom_viewer()
        time.sleep(0.3)
        send_zoom("out")
    return Response(status=204)

@app.route('/create-slideshow', methods=['POST'])
def create_slideshow():
    try:
        data = request.get_json()
        folder = secure_filename(data.get('folderName'))
        if not folder:
            return 'Missing folder name', 400

        real_path = os.path.join('/home/pi/frame-app/static/uploads', folder)
        os.makedirs(real_path, exist_ok=True)

        thumb_path = os.path.join(THUMB_ROOT, 'main', folder)
        os.makedirs(thumb_path, exist_ok=True)

        return '', 204
    except Exception as e:
        app.logger.error(f"Failed to create slideshow folder: {e}")
        return 'Error creating folder', 400

# --- Display mute state persistence ---
DISPLAY_STATE_PATH = os.path.join(BASE_DIR, "display_state.json")

def load_display_state():
    """Safely load display mute state from disk."""
    if os.path.exists(DISPLAY_STATE_PATH):
        try:
            with open(DISPLAY_STATE_PATH, "r") as f:
                return json.load(f)
        except Exception as e:
            app.logger.warning(f"Failed to read display state: {e}")
    return {"displayMuted": False}

def save_display_state(state):
    """Atomically save display mute state to disk."""
    try:
        temp_path = DISPLAY_STATE_PATH + ".tmp"
        with open(temp_path, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, DISPLAY_STATE_PATH)
    except Exception as e:
        app.logger.error(f"Failed to save display state: {e}")

@app.route('/display-toggle', methods=['POST'])
def toggle_display():
    """Toggle the physical display and persist the mute state."""
    data = request.get_json()
    display_muted = data.get("displayMuted", False)

    cmd = ["vcgencmd", "display_power", "0"] if display_muted else \
          ["vcgencmd", "display_power", "1"]

    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        save_display_state({"displayMuted": display_muted})
        return "", 204
    except subprocess.CalledProcessError:
        return "", 500

@app.route("/display-status")
def display_status():
    """Return the current display mute state for frontend sync."""
    state = load_display_state()
    return jsonify(state)

@app.route('/delete', methods=['DELETE', 'POST'])
def delete_image():
    filename = request.args.get('file') or request.form.get('path')
    config = load_config()
    folder = config.get('current_folder')

    # 🛑 Create lock file to block slideshow loop
    lock_path = os.path.join(BASE_DIR, 'deletion.lock')
    try:
        with open(lock_path, 'w') as f:
            f.write('locked')
    except Exception as e:
        app.logger.error(f"Failed to create deletion lock: {e}")

    if not filename or not folder:
        return 'Missing filename or folder', 400

    # Read image list and current viewer position
    order_path = os.path.join(BASE_DIR, 'image_order.txt')
    try:
        with open(order_path, 'r') as f:
            images = f.read().strip().split()

        current_target = os.readlink(SYMLINK_PATH) if os.path.islink(SYMLINK_PATH) else None
        current_name = os.path.basename(current_target) if current_target else None
        current_idx = images.index(current_name) if current_name in images else 0
    except Exception as e:
        app.logger.error(f"Failed to read image_order.txt or symlink: {e}")
        images = []
        current_idx = 0

    # Delete image and thumbnail
    main_path = os.path.join(UPLOAD_ROOT, folder, filename)
    thumb_path = os.path.join(THUMB_ROOT, folder, filename)
    try:
        if os.path.exists(main_path):
            os.remove(main_path)
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
    except Exception as e:
        app.logger.error(f"Error deleting {filename}: {e}")
        return 'Error deleting file', 500

    # Update image list
    images = [img for img in images if img != filename]
    try:
        with open(order_path, 'w') as f:
            f.write('\n'.join(images))
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        app.logger.error(f"Failed to update image_order.txt: {e}")

    # Choose next image based on current viewer position
    if images:
        next_idx = min(current_idx, len(images) - 1)
        next_image_path = os.path.join(UPLOAD_ROOT, folder, images[next_idx])
        update_viewer_state(next_image_path)

        # 🔧 Sync current_filename.txt to match viewer
        try:
            with open(os.path.join(BASE_DIR, 'static', 'current_filename.txt'), 'w') as f:
                f.write(os.path.basename(next_image_path))
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            app.logger.error(f"Failed to update current_filename.txt: {e}")

        # 🕒 Touch delay_updated.flag to reset slideshow timer
        flag_path = os.path.join(BASE_DIR, 'delay_updated.flag')
        try:
            from pathlib import Path
            Path(flag_path).touch()
        except Exception as e:
            app.logger.error(f"Failed to touch delay_updated.flag: {e}")

        refresh_viewer()

    # ✅ Remove deletion lock
    if os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except Exception as e:
            app.logger.error(f"Failed to remove deletion lock: {e}")

    return jsonify({'image': url_for('static', filename='current.jpg')}), 200

@app.route('/save-custom-order', methods=['POST'])
def save_custom_order():
    data = request.get_json()
    folder = data.get('folder')
    order = data.get('order')

    if not folder or not order:
        return 'Missing folder or order', 400

    folder_path = os.path.join(UPLOAD_ROOT, folder)
    os.makedirs(folder_path, exist_ok=True)  # ✅ Ensure directory exists

    order_path = os.path.join(folder_path, 'custom_order.txt')

    try:
        with open(order_path, 'w') as f:
            for filename in order:
                f.write(filename + '\n')
        return 'Order saved', 200
    except Exception as e:
        print(f"Error saving order: {e}")
        return 'Failed to save order', 500


@app.route('/browse')
def browse():
    config = load_config()
    folder = config.get('current_folder')
    if not folder:
        return render_template('Browse.html', images=[], current=None, current_index=0)

    order_path = os.path.join(BASE_DIR, 'image_order.txt')
    try:
        with open(order_path, 'r') as f:
            images = [line.strip() for line in f if line.strip()]
    except Exception as e:
        app.logger.error(f"Failed to read image_order.txt: {e}")
        images = []

    try:
        with open(os.path.join(BASE_DIR, 'static', 'current_filename.txt')) as f:
            current_filename = f.read().strip()
        current_index = images.index(current_filename)
    except Exception as e:
        app.logger.warning(f"Could not resolve current image: {e}")
        current_filename = None
        current_index = 0

    return render_template('Browse.html',
                           images=images,
                           current=current_filename,
                           current_index=current_index,
                           folder=folder)

@app.route('/set_delay', methods=['POST'])
def set_delay():
    try:
        minutes = int(request.form['delay'])
        config = load_config()
        config['delay'] = minutes * 60

        # 🛡️ Protect config write + flag touch
        with config_lock:
            save_config(config)
            Path(os.path.join(BASE_DIR, 'delay_updated.flag')).touch()

        return '', 204
    except Exception as e:
        app.logger.error(f"Failed to set delay: {e}")
        return 'Error', 400


@app.route('/select_folder', methods=['POST'])
def select_folder():
    folder = secure_filename(request.form['folder'])
    config = load_config()
    config['current_folder'] = folder

    with config_lock:
        save_config(config)
        Path(os.path.join(BASE_DIR, 'delay_updated.flag')).touch()

    folder_path = os.path.join(UPLOAD_ROOT, folder)
    images = []

    if folder == 'main':
        order_path = os.path.join(BASE_DIR, 'image_order.txt')
        if os.path.exists(order_path):
            with open(order_path, 'r') as f:
                images = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    else:
        custom_order_path = os.path.join(folder_path, 'custom_order.txt')
        if os.path.exists(custom_order_path):
            with open(custom_order_path, 'r') as f:
                images = [line.strip() for line in f if line.strip()]
        else:
            images = [
                f for f in os.listdir(folder_path)
                if not f.startswith('.') and os.path.isfile(os.path.join(folder_path, f))
            ]
            images.sort()

        # ✅ Write slideshow_list.txt for non-main folders
        create_slideshow_list(folder, images)

    if images:
        first_image_path = os.path.join(folder_path, images[0])
        update_viewer_state(first_image_path, reset_delay=True)

    return '', 204

@app.route('/upload', methods=['POST'])
def upload():
    folder = secure_filename(request.form['folder'])
    folder_path = os.path.join(UPLOAD_ROOT, folder)
    os.makedirs(folder_path, exist_ok=True)

    def get_unique_filename(folder_path, filename):
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(os.path.join(folder_path, filename)):
            filename = f"{base}_{counter}{ext}"
            counter += 1
        return filename

    for file in request.files.getlist('photos'):
        filename = secure_filename(file.filename)
        filename = get_unique_filename(folder_path, filename)
        path = os.path.join(folder_path, filename)
        file.save(path)

        # 🧠 Apply EXIF-safe orientation correction + 3:2 aspect crop
        try:
            with Image.open(path) as img:
                if hasattr(img, '_getexif'):
                    exif = img._getexif()
                    if exif:
                        orientation_tag = next(
                            (tag for tag, name in ExifTags.TAGS.items() if name == 'Orientation'), None)
                        orientation = exif.get(orientation_tag)
                        if orientation == 3:
                            img = img.rotate(180, expand=True)
                        elif orientation == 6:
                            img = img.rotate(270, expand=True)
                        elif orientation == 8:
                            img = img.rotate(90, expand=True)

                width, height = img.size
                desired_ratio = 3 / 2
                current_ratio = width / height

                if current_ratio > desired_ratio:
                    new_width = int(height * desired_ratio)
                    left = (width - new_width) // 2
                    box = (left, 0, left + new_width, height)
                else:
                    new_height = int(width / desired_ratio)
                    top = (height - new_height) // 2
                    box = (0, top, width, top + new_height)

                img = img.crop(box)
                img.save(path)
        except Exception as e:
            app.logger.warning(f"Image processing failed for {filename}: {e}")

        generate_thumbnail(folder, filename)

    return redirect(url_for('index'))


@app.route('/next', methods=['POST'])
def next_image_redirect():
    response = next_image()
    return redirect(url_for('index'))

@app.route('/list-slideshows')
def list_slideshows():
    try:
        folders = sorted([
            f for f in os.listdir(UPLOAD_ROOT)
            if os.path.isdir(os.path.join(UPLOAD_ROOT, f)) and not f.startswith('.')
        ])
        return jsonify(folders)
    except Exception as e:
        app.logger.error(f"Error listing slideshows: {e}")
        return jsonify([]), 500



@app.route('/previous_image')
def previous_image():
    config = load_config()
    folder = config.get('current_folder')
    if not folder:
        return jsonify({'image': None})

    # ✅ Use correct playlist based on folder
    playlist_name = 'image_order.txt' if folder == 'main' else 'slideshow_list.txt'
    order_path = os.path.join(BASE_DIR, playlist_name)

    try:
        with open(order_path, 'r') as f:
            images = [line.strip() for line in f if line.strip()]
    except Exception as e:
        app.logger.error(f"Error reading {playlist_name}: {e}")
        return jsonify({'image': None})

    if not images:
        return jsonify({'image': None})

    try:
        with open(os.path.join(BASE_DIR, 'static', 'current_image.txt')) as f:
            current_name = f.read().strip()
        idx = images.index(current_name)
        prev_idx = (idx - 1) % len(images)
    except Exception as e:
        app.logger.warning(f"Could not find current image in list: {e}")
        prev_idx = 0

    image_path = os.path.join(UPLOAD_ROOT, folder, images[prev_idx])
    update_viewer_state(image_path, reset_delay=False)

    # ✅ Touch delay_updated.flag to reset slideshow timer
    try:
        Path(os.path.join(BASE_DIR, 'delay_updated.flag')).touch()
    except Exception as e:
        app.logger.error(f"Failed to touch delay_updated.flag: {e}")

    return jsonify({'image': url_for('static', filename='current.jpg')})



@app.route('/thumbs/<folder>/<filename>')
def serve_thumbnail(folder, filename):
    thumb_dir = os.path.join(THUMB_ROOT, folder)
    thumb_path = os.path.join(thumb_dir, filename)

    if not os.path.exists(thumb_path):
        result = generate_thumbnail(folder, filename)
        
    return send_from_directory(thumb_dir, filename)

@app.route('/api/thumbnails')
def api_thumbnails():
    config = load_config()
    folder = config.get('current_folder')
    folder_path = os.path.join(UPLOAD_ROOT, folder) if folder else None

    if not folder or not os.path.exists(folder_path):
        return jsonify({"folder": folder, "images": [], "total": 0})

    sort = request.args.get('sort', 'newest')
    ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
    def is_image(filename):
        return os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

    images = [f for f in os.listdir(folder_path)
              if not f.startswith('.') and is_image(f)]

    # ✅ Apply custom order if requested
    custom_order_path = os.path.join(folder_path, 'custom_order.txt')
    if sort == 'custom' and os.path.exists(custom_order_path):
        with open(custom_order_path, 'r') as f:
            custom_order = [line.strip() for line in f if line.strip()]
        ordered = [f for f in custom_order if f in images]
        extras = [f for f in images if f not in ordered]
        images = ordered + sorted(extras)

    # ✅ Apply random (playlist) order if requested
    elif sort == 'random':
        order_path = os.path.join(BASE_DIR, 'image_order.txt')
        if os.path.exists(order_path):
            with open(order_path, 'r') as f:
                playlist_order = [line.strip() for line in f if line.strip()]
            images = [f for f in playlist_order if f in images]
            extras = [f for f in os.listdir(folder_path)
                      if not f.startswith('.') and is_image(f) and f not in images]
            images += sorted(extras)

    elif sort == 'newest':
        images.sort(key=lambda f: os.path.getmtime(os.path.join(folder_path, f)), reverse=True)
    elif sort == 'oldest':
        images.sort(key=lambda f: os.path.getmtime(os.path.join(folder_path, f)))
    elif sort == 'az':
        images.sort()
    elif sort == 'za':
        images.sort(reverse=True)

    total = len(images)

    # Try to read the current filename we write elsewhere; don't let errors break the endpoint
    current_filename = None
    try:
        with open(os.path.join(BASE_DIR, 'static', 'current_filename.txt')) as f:
            current_filename = f.read().strip() or None
    except Exception:
        current_filename = None

    # Compute the index of the current file in the images list (or -1 if not present)
    current_index = -1
    if current_filename and current_filename in images:
        try:
            current_index = images.index(current_filename)
        except Exception:
            current_index = -1

    # Return all images (no pagination) + current filename/index for client optimization
    return jsonify({
        "folder": folder,
        "images": images,
        "total": total,
        "current": current_filename or "",
        "current_index": current_index
    })

@app.route('/show', methods=['POST'])
def show_image():
    filename = secure_filename(request.form['path'])
    config = load_config()
    folder = config.get('current_folder')
    if not folder:
        return 'No folder selected', 400

    image_path = os.path.join(UPLOAD_ROOT, folder, filename)
    if not os.path.exists(image_path):
        return 'Image not found', 404

    update_viewer_state(image_path, reset_delay=False)  # 👈 This now controls the timer reset

    generate_thumbnail(folder, filename)
    refresh_viewer()  # ✅ This is the missing nudge
    return 'OK', 200


def refresh_viewer():
    try:
        subprocess.run(['pkill', '-USR1', 'feh'], check=True)
    except subprocess.CalledProcessError as e:
        app.logger.warning(f"Failed to refresh viewer: {e}")

@app.route('/config')
def get_config():
    config = load_config()
    return jsonify(config)

@app.route('/restart', methods=['POST'])
def restart_pi():
    splash_path = '/usr/share/plymouth/themes/pix/splash.png'
    update_symlink(splash_path)
    time.sleep(0.5)
    subprocess.run(['sudo', 'reboot'])
    return '', 204

@app.route('/next_image')
def next_image():
    config = load_config()
    folder = config.get('current_folder')
    if not folder:
        return jsonify({'image': None})

    playlist_name = 'image_order.txt' if folder == 'main' else 'slideshow_list.txt'
    order_path = os.path.join(BASE_DIR, playlist_name)

    try:
        with open(order_path, 'r') as f:
            images = [line.strip() for line in f if line.strip()]
    except Exception as e:
        app.logger.error(f"Error reading {playlist_name}: {e}")
        return jsonify({'image': None})

    if not images:
        return jsonify({'image': None})

    try:
        with open(os.path.join(BASE_DIR, 'static', 'current_image.txt')) as f:
            current_name = f.read().strip()
        idx = images.index(current_name)
        next_idx = (idx + 1) % len(images)
    except Exception as e:
        app.logger.warning(f"Could not find current image in list: {e}")
        next_idx = 0

    image_path = os.path.join(UPLOAD_ROOT, folder, images[next_idx])
    update_viewer_state(image_path, reset_delay=False)

    try:
        with open(os.path.join(BASE_DIR, 'static', 'current_filename.txt'), 'w') as f:
            f.write(os.path.basename(image_path))
    except Exception as e:
        app.logger.error(f"Failed to update current_filename.txt: {e}")

    try:
        Path(os.path.join(BASE_DIR, 'delay_updated.flag')).touch()
    except Exception as e:
        app.logger.error(f"Failed to touch delay_updated.flag: {e}")

    refresh_viewer()
    return jsonify({'image': url_for('static', filename='current.jpg')})

def slideshow_loop():
    last_flag_mtime = os.path.getmtime('/home/pi/frame-app/delay_updated.flag')
    lock_path = os.path.join(BASE_DIR, 'deletion.lock')

    while True:
        config = load_config()
        delay = config.get('delay', 1200)

        interrupted = False
        for _ in range(delay):
            time.sleep(1)
            current_flag_mtime = os.path.getmtime('/home/pi/frame-app/delay_updated.flag')
            if current_flag_mtime > last_flag_mtime:
                last_flag_mtime = current_flag_mtime
                interrupted = True
                break  # restart sleep with new delay

        if not interrupted:
            # 🛑 Skip this tick if a deletion is in progress
            if os.path.exists(lock_path):
                continue

            try:
                requests.get('http://localhost:5000/next_image')
            except Exception as e:
                app.logger.warning(f"Slideshow loop failed to advance image: {e}")

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    regenerate_image_order()
    logging.info("Shuffled 'main' at Flask launch.")

    # --- NEW: reset "current image" to first in the new random list ---
    order_path = os.path.join(BASE_DIR, 'image_order.txt')
    if os.path.exists(order_path):
        with open(order_path) as f:
            images = [line.strip() for line in f if line.strip()]
        if images:
            first_image = os.path.join(UPLOAD_ROOT, 'main', images[0])
            update_viewer_state(first_image, reset_delay=True)

    threading.Thread(target=slideshow_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
