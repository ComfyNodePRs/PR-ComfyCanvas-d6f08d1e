import torch
from PIL import Image
import struct
import numpy as np
from comfy.cli_args import args, LatentPreviewMethod
from comfy.taesd.taesd import TAESD
import comfy.model_management
import folder_paths
import comfy.utils
import logging
from pathlib import Path
from urllib import request
import base64, io, json

MAX_PREVIEW_RESOLUTION = args.preview_size

def preview_to_image(latent_image,newsample=False):
        latents_ubyte = (((latent_image + 1.0) / 2.0).clamp(0, 1)  # change scale from -1..1 to 0..1
                            .mul(0xFF)  # to 0..255
                            ).to(device="cpu", dtype=torch.uint8, non_blocking=comfy.model_management.device_supports_non_blocking(latent_image.device))
        image = Image.fromarray(newsample)
        try:
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            byte_im = buf.getvalue()
            byte_im = base64.b64encode(byte_im).decode('utf-8')
            byte_im = f"data:image/png;base64,{byte_im}"
            p = {
                "data":{
                            "img":byte_im,
                            "width":image.size[0],
                            "height":image.size[1]
                        }
            }
            data = json.dumps(p).encode('utf-8')
            req =  request.Request("http://localhost:5000/settaesd", data=data)
            req.add_header("Content-Type", "application/json")
            request.urlopen(req)
        except Exception as e: 
            print(e)
        return Image.fromarray(latents_ubyte.numpy())

class LatentPreviewer:
    def decode_latent_to_preview(self, x0):
        pass

    def decode_latent_to_preview_image(self, preview_format, x0):
        preview_image = self.decode_latent_to_preview(x0)
        return ("JPEG", preview_image, MAX_PREVIEW_RESOLUTION)

class TAESDPreviewerImpl(LatentPreviewer):
    def __init__(self, taesd):
        self.taesd = taesd

    def decode_latent_to_preview(self, x0):
        newsample = self.taesd.decode(x0[:1])[0].detach()
        newsample = torch.clamp((newsample + 1.0) / 2.0, min=0.0, max=1.0)
        newsample = 255. * np.moveaxis(newsample.cpu().numpy(), 0, 2)
        newsample = newsample.astype(np.uint8)
        x_sample = self.taesd.decode(x0[:1])[0].movedim(0, 2)
        return preview_to_image(x_sample,newsample)


class Latent2RGBPreviewer(LatentPreviewer):
    def __init__(self, latent_rgb_factors, latent_rgb_factors_bias=None):
        self.latent_rgb_factors = torch.tensor(latent_rgb_factors, device="cpu").transpose(0, 1)
        self.latent_rgb_factors_bias = None
        if latent_rgb_factors_bias is not None:
            self.latent_rgb_factors_bias = torch.tensor(latent_rgb_factors_bias, device="cpu")

    def decode_latent_to_preview(self, x0):
        self.latent_rgb_factors = self.latent_rgb_factors.to(dtype=x0.dtype, device=x0.device)
        if self.latent_rgb_factors_bias is not None:
            self.latent_rgb_factors_bias = self.latent_rgb_factors_bias.to(dtype=x0.dtype, device=x0.device)

        latent_image = torch.nn.functional.linear(x0[0].permute(1, 2, 0), self.latent_rgb_factors, bias=self.latent_rgb_factors_bias)
        # latent_image = x0[0].permute(1, 2, 0) @ self.latent_rgb_factors

        return preview_to_image(latent_image)


def get_previewer(device, latent_format):
    previewer = None
    method = args.preview_method
    if method != LatentPreviewMethod.NoPreviews:
        # TODO previewer methods
        taesd_decoder_path = None
        if latent_format.taesd_decoder_name is not None:
            taesd_decoder_path = next(
                (fn for fn in folder_paths.get_filename_list("vae_approx")
                    if fn.startswith(latent_format.taesd_decoder_name)),
                ""
            )
            taesd_decoder_path = folder_paths.get_full_path("vae_approx", taesd_decoder_path)

        if method == LatentPreviewMethod.Auto:
            method = LatentPreviewMethod.Latent2RGB

        if method == LatentPreviewMethod.TAESD:
            if taesd_decoder_path:
                taesd = TAESD(None, taesd_decoder_path, latent_channels=latent_format.latent_channels).to(device)
                previewer = TAESDPreviewerImpl(taesd)
            else:
                logging.warning("Warning: TAESD previews enabled, but could not find models/vae_approx/{}".format(latent_format.taesd_decoder_name))

        if previewer is None:
            if latent_format.latent_rgb_factors is not None:
                previewer = Latent2RGBPreviewer(latent_format.latent_rgb_factors, latent_format.latent_rgb_factors_bias)
    return previewer

def prepare_callback(model, steps, x0_output_dict=None):
    preview_format = "JPEG"
    if preview_format not in ["JPEG", "PNG"]:
        preview_format = "JPEG"

    previewer = get_previewer(model.load_device, model.model.latent_format)

    pbar = comfy.utils.ProgressBar(steps)
    def callback(step, x0, x, total_steps):
        if x0_output_dict is not None:
            x0_output_dict["x0"] = x0

        preview_bytes = None
        if previewer:
            preview_bytes = previewer.decode_latent_to_preview_image(preview_format, x0)
        pbar.update_absolute(step + 1, total_steps, preview_bytes)
    return callback
