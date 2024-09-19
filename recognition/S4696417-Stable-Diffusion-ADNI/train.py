from torchvision import transforms
from dataset import get_dataloader
from modules import StableDiffusion
from torchvision.datasets import CIFAR10
from torch.utils.data import DataLoader
import torch, wandb, os
import torch.nn as nn
from tqdm import tqdm
from torch.cuda.amp import autocast, GradScaler
import matplotlib.pyplot as plt
import numpy as np
from torchmetrics.functional import structural_similarity_index_measure, peak_signal_noise_ratio

image_transform = transforms.Compose([
    transforms.ToTensor(),    
    transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
])

print("Loading data...")
# os.chdir('recognition/S4696417-Stable-Diffusion-ADNI')
# train_loader, val_loader = get_dataloader('data/train/AD', batch_size=8, transform=image_transform)

# IMport cifar10 dataset from pytorch
train_set = CIFAR10(root='./data', train=True, download=True, transform=image_transform)
test_set = CIFAR10(root='./data', train=False, download=True, transform=image_transform)

train_loader = DataLoader(train_set, batch_size=64, shuffle=True, num_workers=2)
val_loader = DataLoader(test_set, batch_size=64, shuffle=True, num_workers=2)

# Settings
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f'Using {device}')

lr = 0.0001
epochs = 25

print("Loading model...")
model = StableDiffusion(
    in_channels=3,
    out_channels=3,
    model_channels=128,
    num_res_blocks=2,
    attention_resolutions=[16,8],
    channel_mult=[1, 2, 4, 8],
    num_heads=8
)
model = model.to(device)
print("Model loaded")

criterion = nn.MSELoss()
scaler = GradScaler()
optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min')

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
        "name": "SD-CIFAR-10",
    }
)


def generate_images(model, device, epoch, num_images=10):
    print("Generating images...")
    model.eval()
    with torch.no_grad():
        # Generate random noise
        noise = torch.randn(num_images, 3, 32, 32).to(device)
        
        # Generate timestamps (you may need to adjust this based on your model's requirements)
        timestamps = torch.randint(0, 1000, (images.size(0),), device=device).long()
        
        # Generate images
        generated_images = model(noise, timestamps)
        
        # Denormalize and convert to numpy for plotting
        generated_images = generated_images.cpu().numpy()
        generated_images = (generated_images + 1) / 2.0
        generated_images = np.transpose(generated_images, (0, 2, 3, 1))

    # Plot and save the generated images
    fig, axes = plt.subplots(1, num_images, figsize=(20, 2))
    for i, ax in enumerate(axes):
        ax.imshow(generated_images[i])
        ax.axis('off')
    plt.tight_layout()
    plt.savefig(f'generated_images_epoch_{epoch}.png')
    plt.close()

def calculate_gradient_norm(model):
    total_norm = 0
    for p in model.parameters():
        if p.grad is not None:
            param_norm = p.grad.data.norm(2)
            total_norm += param_norm.item() ** 2
    total_norm = total_norm ** 0.5
    return total_norm

def log_param_stats(model):
    for name, param in model.named_parameters():
        if 'weight' in name:
            wandb.log({f'{name}_mean': param.data.mean().item(),
                       f'{name}_std': param.data.std().item()})


print("Training model...")
model.train()
for epoch in range(epochs):
    train_loss, val_loss = 0, 0
    train_psnr, val_psnr = 0, 0
    train_ssim, val_ssim = 0, 0

    loop = tqdm(train_loader, desc=f"Epoch {epoch+1}/{epochs}")
    for i, batch in enumerate(loop):

        # for CIFAR
        images, _ = batch
        images = images.to(device)
        timestamps = torch.randint(0, 1000, (images.size(0),), device=device).long()
       
        optimizer.zero_grad()

        # Mixed precision training
        with autocast():
            outputs = model(images, timestamps)
            loss = criterion(outputs, images)

        scaler.scale(loss).backward()
        grad_norm = calculate_gradient_norm(model)
        scaler.step(optimizer)
        scaler.update()

        # Get PSNR and SSIM metrics
        psnr = peak_signal_noise_ratio(outputs, images)
        ssim = structural_similarity_index_measure(outputs, images)

        train_loss += loss.item()
        train_psnr += psnr.item()
        train_ssim += ssim.item()

        # Log metrics
        wandb.log({
            'train_loss': loss.item(),
            'train_psnr': psnr.item(),
            'train_ssim': ssim.item(),
            'learning_rate': optimizer.param_groups[0]['lr'],
            'gradient_norm': grad_norm
        })

        # update progress bar
        loop.set_postfix(loss=loss.item(), psnr=psnr.item(), ssim=ssim.item())


    scheduler.step(train_loss)
    log_param_stats(model)

    # validation
    model.eval()
    total_loss = 0
    with torch.no_grad():
        for i, batch in enumerate(val_loader):
            # for CIFAR
            images, _ = batch
            timestamps = torch.randint(0, 1000, (images.size(0),), device=device).long()

            #images, timestamps = batch
            images = images.to(device)
            timestamps = timestamps.to(device)

            # Mixed precision training
            with autocast():
                outputs = model(images, timestamps)
                loss = criterion(outputs, images)

            # Calculate PSNR and SSIM
            psnr = peak_signal_noise_ratio(outputs, images)
            ssim = structural_similarity_index_measure(outputs, images)

            val_loss += loss.item()
            val_psnr += psnr.item()
            val_ssim += ssim.item()

            loop.set_postfix(loss=loss.item())

    # Log epoch-level metrics
    wandb.log({
        'epoch': epoch,
        'avg_train_loss': train_loss / len(train_loader),
        'avg_val_loss': val_loss / len(val_loader),
        'avg_train_psnr': train_psnr / len(train_loader),
        'avg_val_psnr': val_psnr / len(val_loader),
        'avg_train_ssim': train_ssim / len(train_loader),
        'avg_val_ssim': val_ssim / len(val_loader)
    })

    print(f'Epoch: {epoch}, Train Loss: {train_loss/len(train_loader):.4f}, Val Loss: {val_loss/len(val_loader):.4f}')
    print(f'Train PSNR: {train_psnr/len(train_loader):.4f}, Val PSNR: {val_psnr/len(val_loader):.4f}')
    print(f'Train SSIM: {train_ssim/len(train_loader):.4f}, Val SSIM: {val_ssim/len(val_loader):.4f}')

    if (epoch + 1) % 5 == 0:
        generate_images(model, device, epoch+1)


wandb.finish()
print("Training complete")
model.save('model.pth')




