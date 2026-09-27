import telebot
from telebot import types
import requests
import json
import time
import random
import threading
from queue import Queue
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_bot.config")) as f:
    for line in f:
        line = line.strip()
        if line.startswith("BOT_TOKEN"):
            BOT_TOKEN = line.split("=", 1)[1].strip().strip('"')
            break

COMFYUI_URL = "http://127.0.0.1:8188"

bot = telebot.TeleBot(BOT_TOKEN)
generation_queue = Queue()

MAX_IMAGES = 9

ASPECTS = {
    "1:1 (Square)": (832, 832),
    "3:2 (Landscape)": (992, 640),
    "2:3 (Portrait)": (640, 992),
    "9:16 (Story)": (544, 960),
}


class UserSession:
    def __init__(self):
        self.state = "idle"
        self.images = []
        self.prompt = ""
        self.negative_prompt = ""
        self.aspect_ratio = "1:1 (Square)"


sessions = {}


def get_session(uid):
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]


def reset_session(uid):
    if uid in sessions:
        for path in sessions[uid].images:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
    sessions[uid] = UserSession()


def build_workflow(prompt, image_filenames, aspect_ratio="1:1 (Square)",
                   negative_prompt="", steps=25, cfg=1, seed=None):
    if seed is None:
        seed = random.randint(0, 999999999999999)

    w, h = ASPECTS.get(aspect_ratio, (832, 832))

    # Determine latent source first (referenced by KSampler)
    if image_filenames:
        latent_source = ["484", 2]
    else:
        latent_source = ["479", 0]

    workflow = {
        "477": {
            "inputs": {
                "clip_name": "qwen3vl_8b_w4a8.safetensors",
                "type": "qwen_image",
                "device": "default"
            },
            "class_type": "CLIPLoader"
        },
        "478": {
            "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"},
            "class_type": "VAELoader"
        },
        "483": {
            "inputs": {
                "device": "auto",
                "dtype": "default",
                "model": ["485", 0]
            },
            "class_type": "QwenImage21Cache"
        },
        "485": {
            "inputs": {"unet_name": "qwen_image_2.1_Q4_K_M.gguf"},
            "class_type": "UnetLoaderGGUF"
        },
        "481": {
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1,
                "model": ["483", 0],
                "positive": ["484", 0],
                "negative": ["484", 1],
                "latent_image": latent_source
            },
            "class_type": "KSampler"
        },
        "480": {
            "inputs": {
                "samples": ["481", 0],
                "vae": ["478", 0]
            },
            "class_type": "VAEDecode"
        },
        "461": {
            "inputs": {
                "filename_prefix": "TGBot_Qwen",
                "images": ["480", 0]
            },
            "class_type": "SaveImage"
        }
    }

    text_encode_inputs = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "clip": ["477", 0]
    }

    for idx, filename in enumerate(image_filenames, start=1):
        node_id = f"70{idx}"
        workflow[node_id] = {
            "inputs": {"image": filename},
            "class_type": "LoadImage"
        }
        text_encode_inputs[f"images.image_{idx}"] = [node_id, 0]

    if image_filenames:
        # Edit path: reference images are VAE-encoded into the latent (out[2]),
        # keep reference resolution.
        text_encode_inputs["vae"] = ["478", 0]
        text_encode_inputs["resolution"] = 0
    else:
        # T2I path: plain text conditioning + empty latent.
        text_encode_inputs["resolution"] = w
        empty = {
            "inputs": {"width": w, "height": h, "batch_size": 1},
            "class_type": "EmptyLatentImage"
        }
        workflow["479"] = empty

    workflow["484"] = {
        "inputs": text_encode_inputs,
        "class_type": "TextEncodeQwenImage21"
    }

    return workflow


def upload_image_to_comfyui(file_path, filename):
    with open(file_path, 'rb') as f:
        files = {'image': (filename, f, 'image/png')}
        response = requests.post(f"{COMFYUI_URL}/upload/image", files=files)
    if response.status_code != 200:
        raise RuntimeError(f"Upload failed: {response.status_code} {response.text}")
    return response.json()['name']


def queue_prompt(workflow):
    response = requests.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
    if response.status_code != 200:
        raise RuntimeError(f"Queue failed: {response.status_code} {response.text}")
    return response.json()['prompt_id']


def wait_for_result(prompt_id, timeout=360):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{COMFYUI_URL}/history/{prompt_id}")
        history = r.json()
        if prompt_id in history:
            if history[prompt_id].get('status', {}).get('status_str') == 'error':
                raise RuntimeError("Generation failed on the ComfyUI side")
            outputs = history[prompt_id]['outputs']
            for node_output in outputs.values():
                if 'images' in node_output:
                    img = node_output['images'][0]
                    return img['filename'], img['subfolder'], img['type']
        time.sleep(2)
    raise TimeoutError("Generation timed out")


def download_image(filename, subfolder, folder_type):
    params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    r = requests.get(f"{COMFYUI_URL}/view", params=params)
    return r.content


@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message,
        "\U0001f3a8 *Qwen Multi-Image Editor Bot*\n\n"
        "/newedit - Start a new image edit (up to 9 images)\n"
        "/cancel - Cancel current session\n\n"
        "Or just send a *single photo with a caption* for quick single-image editing.",
        parse_mode='Markdown')


@bot.message_handler(commands=['cancel'])
def cancel(message):
    uid = message.from_user.id
    reset_session(uid)
    bot.reply_to(message, "\u274c Session cancelled.")


@bot.message_handler(commands=['newedit'])
def new_edit(message):
    uid = message.from_user.id
    reset_session(uid)
    session = get_session(uid)
    session.state = "collecting_images"
    bot.reply_to(message,
        f"\U0001f4f8 Send images one by one (up to {MAX_IMAGES}).\n"
        "I'll number them in the order you send them.")


@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    uid = message.from_user.id
    session = get_session(uid)

    if session.state == "idle":
        if not message.caption:
            bot.reply_to(message,
                "\u26a0\ufe0f For quick mode, add a caption describing the edit.\n"
                "Or use /newedit for multi-image mode.")
            return
        local_path = save_photo(message, uid)
        queue_job(message.chat.id, uid, message.caption, [local_path],
                  session.aspect_ratio)
        return

    if session.state != "collecting_images":
        bot.reply_to(message, "\u26a0\ufe0f Use /newedit to start a new session first.")
        return

    if len(session.images) >= MAX_IMAGES:
        bot.reply_to(message, f"\u26a0\ufe0f Maximum {MAX_IMAGES} images reached.")
        return

    local_path = save_photo(message, uid)
    session.images.append(local_path)
    idx = len(session.images)

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("\u2795 Add Another Image", callback_data="add_more"),
        types.InlineKeyboardButton("\u2705 Done - Enter Prompt", callback_data="finish_images")
    )
    bot.send_message(message.chat.id,
        f"\u2705 Image {idx}/{MAX_IMAGES} received",
        reply_markup=markup)


def save_photo(message, uid):
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded = bot.download_file(file_info.file_path)
    local_path = f"temp_{uid}_{int(time.time()*1000)}.png"
    with open(local_path, 'wb') as f:
        f.write(downloaded)
    return local_path


@bot.callback_query_handler(func=lambda call: call.data == "add_more")
def add_more(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "\U0001f4f8 Send the next image.")


@bot.callback_query_handler(func=lambda call: call.data == "finish_images")
def finish_images(call):
    uid = call.from_user.id
    session = get_session(uid)

    if not session.images:
        bot.answer_callback_query(call.id, "No images added yet!")
        return

    session.state = "awaiting_prompt"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        f"\u2705 {len(session.images)} image(s) locked in.\n\n"
        "\u270d\ufe0f Now send your prompt describing the edit.\n"
        "(You can reference 'image 1', 'image 2' etc. in your prompt)")


@bot.message_handler(func=lambda m: get_session(m.from_user.id).state == "awaiting_prompt")
def handle_prompt(message):
    uid = message.from_user.id
    session = get_session(uid)
    session.prompt = message.text
    session.state = "confirming"
    show_confirmation(message.chat.id, uid)


def show_confirmation(chat_id, uid):
    session = get_session(uid)

    for idx, path in enumerate(session.images, start=1):
        with open(path, 'rb') as f:
            bot.send_photo(chat_id, f, caption=f"\U0001f5bc\ufe0f Image {idx}")

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("\U0001f504 Reorder", callback_data="reorder"),
        types.InlineKeyboardButton("\u270f\ufe0f Edit Prompt", callback_data="edit_prompt")
    )
    markup.row(
        types.InlineKeyboardButton("\U0001f680 Generate", callback_data="generate")
    )

    bot.send_message(chat_id,
        f"\U0001f4cb *Confirm your generation:*\n\n"
        f"Images: {len(session.images)}\n"
        f"Prompt: \"{session.prompt}\"\n"
        f"Aspect: {session.aspect_ratio}\n\n"
        "Does the image order look correct above?",
        parse_mode='Markdown',
        reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "reorder")
def request_reorder(call):
    uid = call.from_user.id
    session = get_session(uid)
    n = len(session.images)
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        f"Send the new order as comma-separated numbers.\n"
        f"Example (for {n} images): {','.join(str(i) for i in range(n, 0, -1))}")
    session.state = "reordering"


@bot.message_handler(func=lambda m: get_session(m.from_user.id).state == "reordering")
def handle_reorder(message):
    uid = message.from_user.id
    session = get_session(uid)
    try:
        order = [int(x.strip()) for x in message.text.split(',')]
        assert sorted(order) == list(range(1, len(session.images) + 1))
        session.images = [session.images[i - 1] for i in order]
        session.state = "confirming"
        bot.reply_to(message, "\u2705 Order updated!")
        show_confirmation(message.chat.id, uid)
    except Exception:
        bot.reply_to(message, "\u26a0\ufe0f Invalid format. Example: 2,1,3")


@bot.callback_query_handler(func=lambda call: call.data == "edit_prompt")
def edit_prompt(call):
    uid = call.from_user.id
    session = get_session(uid)
    session.state = "awaiting_prompt"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "\u270d\ufe0f Send the new prompt.")


@bot.callback_query_handler(func=lambda call: call.data == "generate")
def handle_generate(call):
    uid = call.from_user.id
    session = get_session(uid)
    bot.answer_callback_query(call.id, "Queued!")

    queue_job(call.message.chat.id, uid, session.prompt,
              session.images.copy(), session.aspect_ratio)

    sessions[uid] = UserSession()


def queue_job(chat_id, uid, prompt, image_paths, aspect_ratio):
    generation_queue.put({
        'chat_id': chat_id,
        'user_id': uid,
        'prompt': prompt,
        'image_paths': image_paths,
        'aspect_ratio': aspect_ratio
    })


def process_queue():
    while True:
        if not generation_queue.empty():
            job = generation_queue.get()
            try:
                status = bot.send_message(job['chat_id'],
                    f"\u2699\ufe0f Uploading {len(job['image_paths'])} image(s)...")

                uploaded_filenames = []
                for path in job['image_paths']:
                    fname = os.path.basename(path)
                    uploaded = upload_image_to_comfyui(path, fname)
                    uploaded_filenames.append(uploaded)

                bot.edit_message_text("\U0001f3a8 Generating...",
                                       job['chat_id'], status.message_id)

                workflow = build_workflow(
                    prompt=job['prompt'],
                    image_filenames=uploaded_filenames,
                    aspect_ratio=job['aspect_ratio']
                )
                prompt_id = queue_prompt(workflow)

                out_filename, subfolder, ftype = wait_for_result(prompt_id)
                image_bytes = download_image(out_filename, subfolder, ftype)

                bot.send_photo(job['chat_id'], image_bytes,
                    caption=f"\u2705 Done!\nUsed {len(uploaded_filenames)} image(s)\n"
                            f"Prompt: {job['prompt']}")
                try:
                    bot.delete_message(job['chat_id'], status.message_id)
                except Exception:
                    pass

            except Exception as e:
                try:
                    bot.send_message(job['chat_id'], f"\u274c Error: {str(e)}")
                except Exception:
                    pass

            finally:
                for path in job['image_paths']:
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except Exception:
                            pass

        time.sleep(1)


threading.Thread(target=process_queue, daemon=True).start()

print("Bot is running...")
bot.polling(none_stop=True)