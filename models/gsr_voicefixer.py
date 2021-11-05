import torch.utils
from torchaudio.transforms import MelScale
import torch.utils.data
from voicefixer import Vocoder
from tools.callbacks.base import *
from tools.pytorch.losses import *
from tools.pytorch.pytorch_util import *
from tools.pytorch.random_ import *
from tools.file.wav import *
from dataloaders.augmentation.base import add_noise_and_scale_with_HQ_with_Aug

os.environ['KMP_DUPLICATE_LIB_OK']='True'

class BN_GRU(torch.nn.Module):
    def __init__(self,input_dim,hidden_dim,layer=1, bidirectional=False, batchnorm=True, dropout=0.0):
        super(BN_GRU, self).__init__()
        self.batchnorm = batchnorm
        if(batchnorm):self.bn = nn.BatchNorm2d(1)
        self.gru = torch.nn.GRU(input_size=input_dim,
                hidden_size=hidden_dim,
                num_layers=layer,
                bidirectional=bidirectional,
                dropout=dropout,
                batch_first=True)
        self.init_weights()

    def init_weights(self):
        for m in self.modules():
            if type(m) in [nn.GRU, nn.LSTM, nn.RNN]:
                for name, param in m.named_parameters():
                    if 'weight_ih' in name:
                        torch.nn.init.xavier_uniform_(param.data)
                    elif 'weight_hh' in name:
                        torch.nn.init.orthogonal_(param.data)
                    elif 'bias' in name:
                        param.data.fill_(0)

    def forward(self,inputs):
        # (batch, 1, seq, feature)
        if(self.batchnorm):inputs = self.bn(inputs)
        out,_ = self.gru(inputs.squeeze(1))
        return out.unsqueeze(1)

class Generator(nn.Module):
    def __init__(self,hp,channels):
        super(Generator, self).__init__()
        self.hp = hp
        if(self.hp["gsr"]["voicefixer"]["unet"]):
            from models.components.unet import UNetResComplex_100Mb
            self.analysis_module = UNetResComplex_100Mb(channels=channels)
        elif(self.hp["gsr"]["voicefixer"]["unet_small"]):
            from models.components.unet_small import UNetResComplex_100Mb
            self.analysis_module = UNetResComplex_100Mb(channels=channels)
        elif(self.hp["gsr"]["voicefixer"]["bi_gru"]):
            n_mel = hp["model"]["mel_freq_bins"]
            self.analysis_module = nn.Sequential(
                    nn.BatchNorm2d(1),
                    nn.Linear(n_mel, n_mel * 2),
                    BN_GRU(input_dim=n_mel*2, hidden_dim=n_mel*2, bidirectional=True, layer=2),
                    nn.ReLU(),
                    nn.Linear(n_mel*4, n_mel*2),
                    nn.ReLU(),
                    nn.Linear(n_mel*2, n_mel),
                )
        elif(self.hp["gsr"]["voicefixer"]["dnn"]):
            n_mel = hp["model"]["mel_freq_bins"]
            self.analysis_module = nn.Sequential(
                    nn.Linear(n_mel, n_mel * 2),
                    nn.ReLU(),
                    nn.BatchNorm2d(1),
                    nn.Linear(n_mel * 2, n_mel * 4),
                    nn.ReLU(),
                    nn.BatchNorm2d(1),
                    nn.Linear(n_mel * 4, n_mel * 8),
                    nn.ReLU(),
                    nn.BatchNorm2d(1),
                    nn.Linear(n_mel * 8, n_mel * 4),
                    nn.ReLU(),
                    nn.BatchNorm2d(1),
                    nn.Linear(n_mel * 4, n_mel * 2),
                    nn.ReLU(),
                    nn.Linear(n_mel * 2, n_mel),
                )
    def forward(self, mel_orig):
        out = self.analysis_module(to_log(mel_orig))
        if(type(out) == type({})):
            out = out['mel']
        mel = out + to_log(mel_orig)
        return {'mel': mel}

class VoiceFixer(pl.LightningModule):
    def __init__(self, hp, channels, type_target):
        super(VoiceFixer, self).__init__()

        self.lr = hp["train"]["learning_rate"]
        self.gamma = hp["train"]["lr_decay"]
        self.batch_size = hp["train"]["batch_size"]
        self.input_segment_length = hp["train"]["input_segment_length"]
        self.sampling_rate = hp["data"]["sampling_rate"]
        self.check_val_every_n_epoch = hp["train"]["check_val_every_n_epoch"]
        self.warmup_steps = hp["train"]["warmup_steps"]
        self.reduce_lr_every_n_steps = hp["train"]["reduce_lr_every_n_steps"]

        self.save_hyperparameters()
        self.type_target = type_target
        self.channels = channels
        self.generated = None
        # self.hparams['channels'] = 2
        self.simelspecloss = get_loss_function(loss_type="simelspec")
        self.l1loss = get_loss_function(loss_type="l1")
        self.vocoder = Vocoder(sample_rate=44100)

        self.valid = None
        self.fake = None

        self.train_step = 0
        self.val_step = 0
        self.val_result_save_dir = None
        self.val_result_save_dir_step = None
        self.downsample_ratio = 2 ** 6  # This number equals 2^{#encoder_blcoks}

        self.f_helper = FDomainHelper(
            window_size=hp["model"]["window_size"],
            hop_size=hp["model"]["hop_size"],
            center=True,
            pad_mode=hp["model"]["pad_mode"],
            window=hp["model"]["window"],
            freeze_parameters=True,
        )

        self.mel_freq_bins = hp["model"]["mel_freq_bins"]
        self.mel = MelScale(n_mels=self.mel_freq_bins,
                            sample_rate=self.sampling_rate,
                            n_stft=hp["model"]["window_size"] // 2 + 1)

        # masking
        self.generator = Generator(hp, self.mel_freq_bins)

        self.lr_lambda = lambda step: self.get_lr_lambda(step,
                                                         gamma = self.gamma,
                                                         warmup_steps=self.warmup_steps,
                                                         reduce_lr_every_n_steps=self.reduce_lr_every_n_steps)
        self.hp = hp

    def get_vocoder(self):
        return self.vocoder

    def get_f_helper(self):
        return self.f_helper

    def get_lr_lambda(self, step, gamma, warmup_steps, reduce_lr_every_n_steps):
        r"""Get lr_lambda for LambdaLR. E.g.,

        .. code-block: python
            lr_lambda = lambda step: get_lr_lambda(step, warm_up_steps=1000, reduce_lr_steps=10000)

            from torch.optim.lr_scheduler import LambdaLR
            LambdaLR(optimizer, lr_lambda)
        """
        if step <= warmup_steps:
            return step / warmup_steps
        else:
            return gamma ** (step // reduce_lr_every_n_steps)

    def init_weights(self, module: nn.Module):
        for m in module.modules():
            if type(m) in [nn.GRU, nn.LSTM, nn.RNN]:
                for name, param in m.named_parameters():
                    if 'weight_ih' in name:
                        torch.nn.init.xavier_uniform_(param.data)
                    elif 'weight_hh' in name:
                        torch.nn.init.orthogonal_(param.data)
                    elif 'bias' in name:
                        param.data.fill_(0)

    def pre(self, input):
        sp, _, _ = self.f_helper.wav_to_spectrogram_phase(input)
        mel_orig = self.mel(sp.permute(0,1,3,2)).permute(0,1,3,2)
        return sp, mel_orig

    def forward(self, sp, mel_orig):
        """
        Args:
          input: (batch_size, channels_num, segment_samples)

        Outputs:
          output_dict: {
            'wav': (batch_size, channels_num, segment_samples),
            'sp': (batch_size, channels_num, time_steps, freq_bins)}
        """
        return self.generator(sp, mel_orig)

    def configure_optimizers(self):
        optimizer_g = torch.optim.Adam([{'params': self.generator.parameters()}],
                                       lr=self.lr, amsgrad=True, betas=(0.5, 0.999))

        scheduler_g = {
            'scheduler': torch.optim.lr_scheduler.LambdaLR(optimizer_g, self.lr_lambda),
            'interval': 'epoch',
            'frequency': 1,
        }
        return optimizer_g, scheduler_g

    def preprocess(self, batch, train=False, cutoff=None):
        if(train):
            vocal = batch[self.type_target] # final target
            noise = batch['noise_LR'] # augmented low resolution audio with noise
            augLR = batch[self.type_target+'_aug_LR'] # # augment low resolution audio
            LR = batch[self.type_target+'_LR']
            # embed()
            vocal, LR, augLR, noise = vocal.float().permute(0, 2, 1), LR.float().permute(0, 2, 1), augLR.float().permute(0, 2, 1), noise.float().permute(0, 2, 1)
            # LR, noise = self.add_random_noise(LR, noise)
            snr, scale = [],[]
            for i in range(vocal.size()[0]):
                vocal[i,...], LR[i,...], augLR[i,...], noise[i,...], _snr, _scale = add_noise_and_scale_with_HQ_with_Aug(vocal[i,...],LR[i,...], augLR[i,...], noise[i,...],
                                                                                                                         snr_l=self.hp["augment"]["noise"]["snr_range"][0],
                                                                                                                         snr_h=self.hp["augment"]["noise"]["snr_range"][1],
                                                                                                                         scale_lower=self.hp["augment"]["scale"]["scale_range"][0],
                                                                                                                         scale_upper=self.hp["augment"]["scale"]["scale_range"][1])
                snr.append(_snr), scale.append(_scale)
            return vocal, augLR, LR,  noise + augLR
        else:
            if(cutoff is None):
                LR_noisy = batch["noisy"]
                LR = batch["vocals"]
                vocals = batch["vocals"]
                vocals, LR, LR_noisy = vocals.float().permute(0, 2, 1), LR.float().permute(0, 2, 1), LR_noisy.float().permute(0, 2, 1)
                return vocals, LR, LR_noisy, batch['fname'][0]
            else:
                LR_noisy = batch["noisy"+"LR"+"_"+str(cutoff)]
                LR = batch["vocals" + "LR" + "_" + str(cutoff)]
                vocals = batch["vocals"]
                vocals, LR, LR_noisy = vocals.float().permute(0, 2, 1), LR.float().permute(0, 2, 1), LR_noisy.float().permute(0, 2, 1)
                return vocals, LR, LR_noisy, batch['fname'][0]


    def training_step(self, batch, batch_nb, optimizer_idx):

        self.vocal, self.augLR, _, self.LR_noisy = self.preprocess(batch, train=True)

        # for i in range(self.vocal.size()[0]):
        #     save_wave(tensor2numpy(self.vocal[i, ...]), str(i) + "vocal" + ".wav", sample_rate=44100)
        #     save_wave(tensor2numpy(self.LR_noisy[i, ...]), str(i) + "LR_noisy" + ".wav", sample_rate=44100)

        _, self.mel_target = self.pre(self.vocal)
        self.sp_LR_target_noisy, self.mel_LR_target_noisy = self.pre(self.LR_noisy)

        self.generated = self(self.sp_LR_target_noisy, self.mel_LR_target_noisy)

        targ_loss = self.l1loss(self.generated['mel'], to_log(self.mel_target))

        self.log("targ-l", targ_loss, on_step=True, on_epoch=False, logger=True, sync_dist=True, prog_bar=True)
        loss = targ_loss
        self.train_step += 1.0
        return {"loss": loss}