import torch, wandb, os
import torch.nn as nn
import torch.optim as optim
from dataset import get_dataloader
from torchvision import transforms
from tqdm import tqdm
from vae import VAE
from torchmetrics.functional.image import structural_similarity_index_measure, peak_signal_noise_ratio

def vae_loss(recon_x, x, mu, logvar, kld_weight=0.1):
    batch_size = x.size(0)
    MSE = nn.functional.mse_loss(recon_x, x, reduction='sum') / batch_size
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp()) / batch_size
    loss = MSE + kld_weight * KLD
    return loss, MSE, KLD

def calculate_metrics(original, reconstructed):
    ssim = structural_similarity_index_measure(reconstructed, original)
    psnr = peak_signal_noise_ratio(reconstructed, original)
    return ssim, psnr

def pretrain_vae(vae, 
                 train_loader, 
                 val_loader, 
                 num_epochs, 
                 device, 
                 batch_size, 
                 image_size,
                 ckp_path):
    
    vae.to(device)
    optimizer = optim.AdamW(vae.parameters(), lr=1e-3, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=5, factor=0.5)

    for epoch in range(num_epochs):
        vae.train()
        train_loss, train_mse, train_kld = 0, 0, 0
        
        for data, _ in tqdm(train_loader, desc=f"Epoch {epoch+1}/{num_epochs}"):
            data = data.to(device)
            optimizer.zero_grad()

            recon_images, mu, logvar = vae(data)
            loss, mse, kld = vae_loss(recon_images, data, mu, logvar)
            
            loss.backward()
            torch.nn.utils.clip_grad_norm_(vae.parameters(), max_norm=1.0)
            optimizer.step()
            
            train_loss += loss.item()
            train_mse += mse.item()
            train_kld += kld.item()

        vae.eval()
        val_loss, val_mse, val_kld = 0, 0, 0
        with torch.no_grad():
            for data, _ in val_loader:
                data = data.to(device)

                recon_images, mu, logvar = vae(data)

                loss, mse, kld = vae_loss(recon_images, data, mu, logvar)
                val_loss += loss.item()
                val_mse += mse.item()
                val_kld += kld.item()

        # Normalize metrics
        num_train_batches = len(train_loader)
        num_val_batches = len(val_loader)
        train_metrics = [x / num_train_batches for x in [train_loss, train_mse, train_kld]]
        val_metrics = [x / num_val_batches for x in [val_loss, val_mse, val_kld]]

        print(f'Epoch: {epoch+1}')
        print(f'Train - Loss: {train_metrics[0]:.4f}, MSE: {train_metrics[1]:.4f}')
        print(f'Val   - Loss: {val_metrics[0]:.4f}, MSE: {val_metrics[1]:.4f}')
        
        wandb.log({
            "epoch": epoch,
            "train_loss": train_metrics[0],
            "train_mse": train_metrics[1],
            "train_kld": train_metrics[2],
            "val_loss": val_metrics[0],
            "val_mse": val_metrics[1],
            "val_kld": val_metrics[2],
            "lr": optimizer.param_groups[0]['lr']
        })

        scheduler.step(val_metrics[0])

        # Log reconstructed images periodically
        if epoch % 1 == 0:
            with torch.no_grad():
                sample = next(iter(val_loader))[0][:8].to(device)
                recon, _, _ = vae(sample)
                comparison = torch.cat([sample, recon])
                wandb.log({
                    "reconstructions": wandb.Image(comparison.cpu())
                })

    path = os.path.join(os.getcwd(), f'{ckp_path}/ADNI-vae_e{epoch+1}_b{batch_size}_im{image_size}.pt')
    torch.save(vae, path)
    print("Pretraining completed. VAE saved.")

def train_vae(IMAGE_SIZE=128, 
              epochs=80, 
              batch_size=16, 
              data_path='/home/groups/comp3710/ADNI/AD_NC/train/AD',
              ckp_path='checkpoints/VAE'):
    # Initialize wandb
    wandb.init(project="vae-pretraining", name=f"VAE-ADNI Pretraining ({IMAGE_SIZE})",
               config={
                "epochs": epochs,
                "name": "VAE-ADNI Pretraining",
                "image size": IMAGE_SIZE,
                "batch size": batch_size
    })

    # Set up data loaders
    image_transform = transforms.Compose([
    transforms.ToPILImage(),
    transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize((0.5,), (0.5,))
    ])


    train_loader, val_loader = get_dataloader(data_path, batch_size=16, transform=image_transform)

    # Initialize VAE (assuming you have a VAE class defined)
    vae = VAE(in_channels=1, latent_dim=8) 

    # Set device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Pretrain VAE
    pretrain_vae(vae, 
                 train_loader, 
                 val_loader, 
                 num_epochs=80, 
                 device=device, 
                 batch_size=16, 
                 image_size=IMAGE_SIZE, 
                 ckp_path=ckp_path)

    wandb.finish()

######### Pre Train VAE Model #########

method = 'Local'
epochs = 80
batch_size = 16
IMAGE_SIZE = 128

if method == 'Slurm':
    train_vae(IMAGE_SIZE, epochs, batch_size, 
              data_path='/home/groups/comp3710/ADNI/AD_NC/train/AD', 
              ckp_path='checkpoints/VAE')
elif method == 'Local':
    train_vae(IMAGE_SIZE, epochs, batch_size, 
              data_path='recognition/S4696417-Stable-Diffusion-ADNI/data/train/AD', 
              ckp_path='recognition/S4696417-Stable-Diffusion-ADNI/checkpoints/VAE')

