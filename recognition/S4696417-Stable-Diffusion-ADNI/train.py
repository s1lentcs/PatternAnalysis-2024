from torchvision import transforms
from dataset import get_dataloader
from modules import StableDiffusion, NoiseScheduler, UNet, VAE
from torchvision.datasets import MNIST
from torch.utils.data import DataLoader
import torch, wandb, os, io
import torch.nn as nn
from tqdm import tqdm
import torch.nn.functional as F
from torch.cuda.amp import autocast, GradScaler
from utils import generate_samples
from pre_train import train_vae
from torchmetrics.functional.image import peak_signal_noise_ratio, structural_similarity_index_measure

image_transform = transforms.Compose([
    transforms.Resize((256, 256)),
    transforms.ToTensor(),    
    # transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
    transforms.Normalize((0.1307,), (0.3081,)) # MNIST-specific
])

print("Loading data...")
# os.chdir('recognition/S4696417-Stable-Diffusion-ADNI')
# train_loader, val_loader = get_dataloader('data/train/AD', batch_size=8, transform=image_transform)

# IMport MNIST dataset
train_set = MNIST(root='./data', train=True, download=True, transform=image_transform)
test_set = MNIST(root='./data', train=False, download=True, transform=image_transform)

train_loader = DataLoader(train_set, batch_size=64, shuffle=True, num_workers=2)
val_loader = DataLoader(test_set, batch_size=64, shuffle=True, num_workers=2)

# Settings
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f'Using {device}')

lr = 1e-5
epochs = 100

print("Loading model...")
#train_vae()
vae = VAE(in_channels=1, latent_dim=16)
vae.load_state_dict(torch.load('pretrained_vae.pth'))
vae.eval() 
for param in vae.parameters():
    param.requires_grad = False 

unet = UNet(in_channels=16, hidden_dims=[32, 64, 128, 256], time_emb_dim=256)
noise_scheduler = NoiseScheduler().to(device)
model = StableDiffusion(unet, vae, noise_scheduler).to(device)

criterion = nn.MSELoss()
scaler = GradScaler()
optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

# initialise wandb
wandb.init(
    project="Stable-Diffusion-ADNI", 
    entity="s1lentcs-uq",
    config={
        "learning rate": lr,
        "epochs": epochs,
        "optimizer": type(optimizer).__name__,
        "scheduler": type(scheduler).__name__,
        "loss": type(criterion).__name__,
        "scaler": type(scaler).__name__,
        "name": "SD-MNIST - VAE and Unet",
    })

print("Training model...")
for epoch in range(epochs):
    model.train()
    train_loss, val_loss = 0, 0
    train_psnr, val_psnr = 0, 0
    train_ssim, val_ssim = 0, 0
    train_loss, vae_train_loss, unet_train_loss = 0, 0, 0

    loop = tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs}")
    for i, batch in enumerate(loop):

        # for MNIST
        images, _ = batch # retrieve clean image batch
        images = images.to(device)

        # Encode images to latent space
        with torch.no_grad():
            latents = model.vae.encode_to_latent(images)
        
        # Sample noise and timesteps
        noise = torch.randn_like(latents)
        timesteps = torch.randint(0, noise_scheduler.num_timesteps, (images.size(0),), device=device)
        
        # Add noise to latents
        noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

        # Train UNet
        optimizer.zero_grad()
        with autocast():
            predicted_noise = model.unet(noisy_latents, timesteps)
            loss = criterion(predicted_noise, noise)

        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()

        # Compute metrics
        with torch.no_grad():
            # denoised_latents = noise_scheduler.remove_noise(noisy_latents, predicted_noise, timesteps)
            # denoised_images = model.decode(denoised_latents)
            denoised_latents = noise_scheduler.step(predicted_noise, timesteps, noisy_latents)
            denoised_images = model.vae.decode_from_latent(denoised_latents)
            ssim = structural_similarity_index_measure(denoised_images, images)
            psnr = peak_signal_noise_ratio(denoised_images, images)

        # Update metrics
        train_loss += loss.item()
        train_psnr += psnr.item()
        train_ssim += ssim.item()

        # Update progress bar
        loop.set_postfix(loss=loss.item(), psnr=psnr.item(), ssim=ssim.item())

        # Log metrics
        wandb.log({
            'train_loss': loss.item(),
            'train_psnr': psnr.item(),
            'train_ssim': ssim.item(),
            'learning_rate': optimizer.param_groups[0]['lr'],
        })

    # Compute average metrics
    avg_train_loss = train_loss / len(train_loader)
    avg_train_psnr = train_psnr / len(train_loader)
    avg_train_ssim = train_ssim / len(train_loader)

    # Log average loss for the epoch
    avg_train_loss = train_loss / len(train_loader)
    wandb.log({"epoch": epoch, "train_loss": avg_train_loss})
    
    # Generate and log sample images
    # if (epoch + 1) % 5 == 0:  # Generate every 5 epochs
    #     generate_samples(model, noise_scheduler, device, epoch+1)

    # Validation
    model.eval()
    val_loss = 0
    val_psnr = 0
    val_ssim = 0
    with torch.no_grad():
        for images, _ in val_loader:
            images = images.to(device)

            with torch.no_grad():
                latents = model.vae.encode_to_latent(images)

            noise = torch.randn_like(latents)
            timesteps = torch.randint(0, noise_scheduler.num_timesteps, (images.size(0),), device=device)
            noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)

            with autocast():
                predicted_noise = model.unet(noisy_latents, timesteps)
                loss = criterion(predicted_noise, noise)

            with torch.no_grad():
                # denoised_latents = noise_scheduler.remove_noise(noisy_latents, predicted_noise, timesteps)
                # denoised_images = model.decode(denoised_latents)
                denoised_latents = noise_scheduler.step(predicted_noise, timesteps, noisy_latents)
                denoised_images = model.vae.decode_from_latent(denoised_latents)
                ssim = structural_similarity_index_measure(denoised_images, images)
                psnr = peak_signal_noise_ratio(denoised_images, images)

            val_loss += loss.item()
            val_psnr += psnr.item()
            val_ssim += ssim.item()
    
    # Compute average validation metrics
    avg_val_loss = val_loss / len(val_loader)
    avg_val_psnr = val_psnr / len(val_loader)
    avg_val_ssim = val_ssim / len(val_loader)

    # Log epoch-level metrics
    wandb.log({
        'epoch': epoch,
        'avg_train_loss': avg_train_loss,
        'avg_val_loss': avg_val_loss,
        'avg_train_psnr': avg_train_psnr,
        'avg_val_psnr': avg_val_psnr,
        'avg_train_ssim': avg_train_ssim,
        'avg_val_ssim': avg_val_ssim
    })

    print(f'Epoch: {epoch}, Train Loss: {avg_train_loss:.4f}, Val Loss: {avg_val_loss:.4f}')
    print(f'Train PSNR: {avg_train_psnr:.4f}, Val PSNR: {avg_val_psnr:.4f}')
    print(f'Train SSIM: {avg_train_ssim:.4f}, Val SSIM: {avg_val_ssim:.4f}')

    # Generate and log sample images
    if (epoch + 1) % 2 == 0:  # Generate every 5 epochs
        generate_samples(model, noise_scheduler, device, epoch+1)

    # Step the scheduler
    scheduler.step()

print("Training complete")
torch.save(model.state_dict(), 'final_model.pth')
wandb.finish()





