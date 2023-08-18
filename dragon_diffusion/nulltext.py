# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/02_nulltext.ipynb.

# %% ../nbs/02_nulltext.ipynb 2
from __future__ import annotations
import math, random, torch, matplotlib.pyplot as plt, numpy as np, matplotlib as mpl, shutil, os, gzip, pickle, re, copy
from pathlib import Path
from operator import itemgetter
from itertools import zip_longest
from functools import partial
import fastcore.all as fc
from glob import glob

from torch import tensor, nn, optim
import torch.nn.functional as F
from tqdm.auto import tqdm
import torchvision.transforms.functional as TF
from torch.optim import lr_scheduler
from diffusers import UNet2DModel
from torch.utils.data import DataLoader, default_collate
from torch.nn import init

from einops import rearrange
from fastprogress import progress_bar
from PIL import Image
from torchvision.io import read_image,ImageReadMode
from torchvision import transforms

from dataclasses import dataclass
from diffusers import LMSDiscreteScheduler, UNet2DConditionModel, AutoencoderKL, DDIMScheduler
from transformers import AutoTokenizer, CLIPTextModel
from diffusers.utils import BaseOutput

from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.utils import BaseOutput, deprecate
from diffusers.schedulers.scheduling_utils import SchedulerMixin

from .core import *
from .masks import *

# %% auto 0
__all__ = ['ddim_inversion', 'null_text_inversion', 'reconstruct']

# %% ../nbs/02_nulltext.ipynb 5
@torch.no_grad()
def ddim_inversion(latents, cond_embeddings, scheduler, model):
    next_latents = latents
    all_latents = [latents.detach().cpu()]

    for t, next_t in progress_bar([(i,j) for i,j in zip(reversed(scheduler.timesteps[1:]), reversed(scheduler.timesteps[:-1]))], leave=False, comment='inverting image...'):
        latent_model_input = scheduler.scale_model_input(next_latents, t)
        noise_pred = model(latent_model_input, t, encoder_hidden_states=cond_embeddings).sample

        alpha_prod_t =  scheduler.alphas_cumprod[t]
        alpha_prod_t_next = scheduler.alphas_cumprod[next_t]
        beta_prod_t = 1 - alpha_prod_t
        beta_prod_t_next = 1 - alpha_prod_t_next

        f = (next_latents - beta_prod_t ** 0.5 * noise_pred) / (alpha_prod_t ** 0.5)
        next_latents = alpha_prod_t_next ** 0.5 * f + beta_prod_t_next ** 0.5 * noise_pred
        all_latents.append(next_latents.detach().cpu())

    return all_latents

# %% ../nbs/02_nulltext.ipynb 7
def null_text_inversion(model, scheduler, all_latents, embeddings, inner_steps=10, lr=0.01, guidance=7.5, generator=None, device='cuda'):
    cond_embeddings, uncond_embeddings = embeddings.chunk(2)
    
    # set up uncond_embeddings as a parameter
    uncond_embeddings = torch.nn.Parameter(uncond_embeddings, requires_grad=True)
    uncond_embeddings = uncond_embeddings.detach()
    uncond_embeddings.requires_grad_(True)

    # optimizer
    optimizer = optim.Adam(
        [uncond_embeddings],
        lr=lr,
    )
    
    cond_embeddings = cond_embeddings.detach()
    results = []
    latents = all_latents[-1].to(device)
    
    for t, prev_latents in progress_bar([(i,j) for i,j in zip(scheduler.timesteps, reversed(all_latents[:-1]))], leave=False, comment='null text optimising...'):
        prev_latents = prev_latents.to(device).detach()
        latent_model_input = scheduler.scale_model_input(latents, t).detach()
        cond = model(latent_model_input, t, encoder_hidden_states=cond_embeddings).sample.detach()
        for _ in progress_bar(range(inner_steps), leave=False):
            uncond = model(latent_model_input, t, encoder_hidden_states=uncond_embeddings).sample
            noise_pred = uncond + guidance * (cond - uncond)
            
            prev_latents_pred = scheduler.step(noise_pred, t, latents).prev_sample
            loss = F.mse_loss(prev_latents_pred, prev_latents).mean()
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()

        results.append(cond_embeddings.detach().cpu())
        latents = prev_latents_pred.detach()
        
    return all_latents[-1], results

# %% ../nbs/02_nulltext.ipynb 9
@torch.no_grad()
def reconstruct(model, scheduler, latents, cond_embeddings, null_uncond_embeddings, guidance=7.5, generator=None, eta=0.0, device='cuda', vae=None, decode=False):
    if decode: assert vae is not None
    latents = latents.to(device)
    for i, (t, null_embed) in enumerate(progress_bar([(i,j) for i, j in zip(scheduler.timesteps, null_uncond_embeddings)], leave=False, comment='reconstructing image...')):
        latent_model_input = torch.cat([latents] * 2)
        latent_model_input = scheduler.scale_model_input(latent_model_input, t)
        embeddings = torch.cat([null_embed.to(device), cond_embeddings])
        
        noise_pred = model(latent_model_input, t, encoder_hidden_states=embeddings).sample
        uncond, cond = noise_pred.chunk(2)
        noise_pred = uncond + guidance * (cond - uncond)
       
        latents = scheduler.step(noise_pred, t, latents).prev_sample
    
    if decode:
        image = decode_img(latents, vae)
        return image
    return latents
