"""
This code is based on Facebook's HDemucs code: https://github.com/facebookresearch/demucs
"""

import json
import logging
import shutil
from pathlib import Path
import os
import time
import wandb

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torchaudio.transforms

from src.ddp import distrib
from src.data.datasets import PrHrSet, match_signal
from src.enhance import enhance, save_wavs, save_specs
from src.evaluate import evaluate, evaluate_on_saved_data
from src.log_results import log_results
from src.models.discriminators import MultiPeriodDiscriminator, MultiScaleDiscriminator, discriminator_loss, feature_loss, \
    generator_loss
from src.models.stft_loss import MultiResolutionSTFTLoss
from src.utils import bold, copy_state, pull_metric, serialize_model, swap_state, LogProgress

from torchaudio.functional import resample

logger = logging.getLogger(__name__)

SERIALIZE_KEY_MODELS = 'models'
SERIALIZE_KEY_OPTIMIZERS = 'optimizers'
SERIALIZE_KEY_HISTORY = 'history'
SERIALIZE_KEY_STATE = 'state'
SERIALIZE_KEY_BEST_STATES = 'best_states'
SERIALIZE_KEY_ARGS = 'args'

GENERATOR_KEY = 'generator'
GENERATOR_OPTIMIZER_KEY = 'generator_optimizer'

METRICS_KEY_EVALUATION_LOSS = 'evaluation_loss'
METRICS_KEY_BEST_LOSS = 'best_loss'

METRICS_KEY_LSD = 'Average lsd'
METRICS_KEY_VISQOL = 'Average visqol'


class Solver(object):
    def __init__(self, data, models, optimizers, args):
        self.tr_loader = data['tr_loader']
        self.cv_loader = data['cv_loader']
        self.tt_loader = data['tt_loader']
        self.args = args

        self.adversarial_mode = 'adversarial' in args.experiment and args.experiment.adversarial
        self.multiple_discriminators_mode = 'multiple_discriminators' in args.experiment and args.experiment.multiple_discriminators
        self.joint_disc_optimizers = 'joint_disc_optimizers' in self.args.experiment and self.args.experiment.joint_disc_optimizers

        self.models = models
        self.dmodels = {k: distrib.wrap(model) for k, model in models.items()}
        self.model = self.models['generator']
        self.dmodel = self.dmodels['generator']


        self.optimizers = optimizers
        self.optimizer = optimizers['optimizer']
        if self.adversarial_mode:
            if self.multiple_discriminators_mode and not self.joint_disc_optimizers:
                self.disc_optimizers = optimizers['discriminator']
            else:
                self.disc_optimizers = {'disc_optimizer': optimizers['disc_optimizer']}


        # Training config
        self.device = args.device
        self.epochs = args.epochs

        # Checkpoints
        self.continue_from = args.continue_from
        self.eval_every = args.eval_every
        self.cross_valid = args.cross_valid
        self.cross_valid_every = args.cross_valid_every
        self.checkpoint = args.checkpoint
        if self.checkpoint:
            self.checkpoint_file = Path(args.checkpoint_file)
            self.best_file = Path(args.best_file)
            logger.debug("Checkpoint will be saved to %s", self.checkpoint_file.resolve())
        self.history_file = args.history_file

        self.best_states = None
        self.restart = args.restart
        self.history = []  # Keep track of loss
        self.samples_dir = args.samples_dir  # Where to save samples


        self.num_prints = args.num_prints  # Number of times to log per epoch

        if 'stft' in self.args.losses:
            self.mrstftloss = MultiResolutionSTFTLoss(factor_sc=args.stft_sc_factor,
                                                  factor_mag=args.stft_mag_factor).to(self.device)
        if 'mbd' in self.args.losses:
            self.melspec_transform = torchaudio.transforms.MelSpectrogram(
                self.args.experiment.hr_sr,
                **self.args.experiment.mel_spectrogram).to(self.device)

        if 'mel_spec_transform' in self.args.experiment and self.args.experiment.mel_spec_transform:
            self.melspec_transform = torchaudio.transforms.MelSpectrogram(
                self.args.experiment.hr_sr,
                **self.args.experiment.mel_spectrogram).to(self.device)

        if 'discriminator_model' in self.args.experiment and \
                (self.args.experiment.discriminator_model == 'hifi' or self.args.experiment.discriminator_model == 'mbd'):
            self.melspec_transform = torchaudio.transforms.MelSpectrogram(
                                            self.args.experiment.hr_sr,
                                            **self.args.experiment.mel_spectrogram).to(self.device)

        self._reset()

    def _copy_models_states(self):
        states = {}
        for name, model in self.models.items():
            states[name] = copy_state(model.state_dict())
        return states

    def _serialize_models(self):
        serialized_models = {}
        for name, model in self.models.items():
            serialized_models[name] = serialize_model(model)
        return serialized_models

    def _serialize_optimizers(self):
        serialized_optimizers = {}
        if self.multiple_discriminators_mode and not self.joint_disc_optimizers:
            serialized_optimizers['optimizer'] = self.optimizers['optimizer'].state_dict()
            serialized_optimizers.update({'discriminator': {}})
            for name, optimizer in self.optimizers['discriminator'].items():
                serialized_optimizers['discriminator'][name] = optimizer.state_dict()
        else:
            for name, optimizer in self.optimizers.items():
                serialized_optimizers[name] = optimizer.state_dict()
        return serialized_optimizers

    def _serialize(self):
        package = {}
        package[SERIALIZE_KEY_MODELS] = self._serialize_models()
        package[SERIALIZE_KEY_OPTIMIZERS] = self._serialize_optimizers()
        package[SERIALIZE_KEY_HISTORY] = self.history
        package[SERIALIZE_KEY_BEST_STATES] = self.best_states
        package[SERIALIZE_KEY_ARGS] = self.args
        tmp_path = str(self.checkpoint_file) + ".tmp"
        torch.save(package, tmp_path)
        # renaming is sort of atomic on UNIX (not really true on NFS)
        # but still less chances of leaving a half written checkpoint behind.
        os.rename(tmp_path, self.checkpoint_file)

        # Saving only the latest best model.
        models = package[SERIALIZE_KEY_MODELS]
        for model_name, best_state in package[SERIALIZE_KEY_BEST_STATES].items():
            models[model_name][SERIALIZE_KEY_STATE] = best_state
            model_filename = model_name + '_' + self.best_file.name
            tmp_path = os.path.join(self.best_file.parent, model_filename) + ".tmp"
            torch.save(models[model_name], tmp_path)
            model_path = Path(self.best_file.parent / model_filename)
            os.rename(tmp_path, model_path)


    def _load(self, package, load_best=False):
        if load_best:
            for name, model_package in package[SERIALIZE_KEY_BEST_STATES][SERIALIZE_KEY_MODELS].items():
                self.models[name].load_state_dict(model_package[SERIALIZE_KEY_STATE])
        else:
            for name, model_package in package[SERIALIZE_KEY_MODELS].items():
                self.models[name].load_state_dict(model_package[SERIALIZE_KEY_STATE])
            if self.multiple_discriminators_mode and not self.joint_disc_optimizers:
                self.optimizers['optimizer'].load_state_dict(package[SERIALIZE_KEY_OPTIMIZERS]['optimizer'])
                for name, opt_package in package[SERIALIZE_KEY_OPTIMIZERS]['discriminator'].items():
                    self.optimizers['discriminator'][name].load_state_dict(opt_package)
            else:
                for name, opt_package in package[SERIALIZE_KEY_OPTIMIZERS].items():
                    self.optimizers[name].load_state_dict(opt_package)

    def _reset(self):
        """_reset."""
        load_from = None
        load_best = False
        keep_history = True
        # Reset
        if self.checkpoint and self.checkpoint_file.exists() and not self.restart:
            load_from = self.checkpoint_file
        elif self.continue_from:
            load_from = self.continue_from
            load_best = self.args.continue_best
            keep_history = self.args.keep_history

        if load_from:
            logger.info(f'Loading checkpoint model: {load_from}')
            package = torch.load(load_from, 'cpu')
            self._load(package, load_best)
            if keep_history:
                self.history = package[SERIALIZE_KEY_HISTORY]
            self.best_states = package[SERIALIZE_KEY_BEST_STATES]


    def train(self):
        # Optimizing the model
        if self.history:
            logger.info("Replaying metrics from previous run")
        for epoch, metrics in enumerate(self.history):
            info = " ".join(f"{k.capitalize()}={v:.5f}" for k, v in metrics.items())
            logger.info(f"Epoch {epoch + 1}: {info}")

        logger.info('-' * 70)
        logger.info("Trainable Params:")
        for name, model in self.models.items():
            n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
            mb = n_params * 4 / 2 ** 20
            logger.info(f"{name}: parameters: {n_params}, size: {mb} MB")

        torch.set_num_threads(1)

        best_loss = None
        self.best_states = {}

        for epoch in range(len(self.history), self.epochs):
            # Train one epoch
            self.model.train()
            start = time.time()
            logger.info('-' * 70)
            logger.info("Training...")
            losses = self._run_one_epoch(epoch)
            logger_msg = f'Train Summary | End of Epoch {epoch + 1} | Time {time.time() - start:.2f}s | ' \
                         + ' | '.join([f'{k} Loss {v:.5f}' for k, v in losses.items()])
            logger.info(bold(logger_msg))
            losses = {k + '_loss': v for k, v in losses.items()}
            valid_losses = {}
            evaluation_loss = None

            evaluated_on_test_data = False

            if self.cross_valid and ((epoch + 1) % self.cross_valid_every == 0 or epoch == self.epochs - 1)\
                    and self.cv_loader:
                # Cross validation
                cross_valid_start = time.time()
                logger.info('-' * 70)
                logger.info('Cross validation...')
                self.model.eval()
                with torch.no_grad():
                    if self.args.valid_equals_test:
                        evaluate_on_test_data = (epoch + 1) % self.eval_every == 0 or epoch == self.epochs - 1 and self.tt_loader
                        valid_losses, enhanced_filenames = self._get_valid_losses_on_test_data(epoch,
                                                                                       enhance=evaluate_on_test_data)
                        evaluated_on_test_data = True
                    else:
                        valid_losses = self._run_one_epoch(epoch, cross_valid=True)
                evaluation_loss = valid_losses['evaluation']
                logger_msg = f'Validation Summary | End of Epoch {epoch + 1} | Time {time.time() - cross_valid_start:.2f}s | ' \
                             + ' | '.join([f'{k} Valid Loss {v:.5f}' for k, v in valid_losses.items()])
                logger.info(bold(logger_msg))
                valid_losses = {'valid_' + k + '_loss': v for k, v in valid_losses.items()}

                best_loss = min(pull_metric(self.history, 'valid_evaluation_loss') + [evaluation_loss])
                # Save the best model
                if evaluation_loss == best_loss:
                    logger.info(bold('New best valid loss %.4f'), evaluation_loss)
                    self.best_states = self._copy_models_states()



            metrics = {**losses, **valid_losses}

            if evaluation_loss:
                metrics.update({METRICS_KEY_EVALUATION_LOSS: evaluation_loss})

            if best_loss:
                metrics.update({METRICS_KEY_BEST_LOSS: best_loss})

            # evaluate and enhance samples every 'eval_every' argument number of epochs
            # also evaluate on last epoch
            if ((epoch + 1) % self.eval_every == 0 or epoch == self.epochs - 1) and self.tt_loader:

                # Evaluate on the testset
                logger.info('-' * 70)
                logger.info('Evaluating on the test set...')
                # If best state exists and evalute_on_best configured, we switch to the best known model for testing.
                # Otherwise we use last state
                if self.args.evaluate_on_best and self.best_states:
                    logger.info('Loading best state.')
                    best_state = self.best_states[GENERATOR_KEY]
                else:
                    logger.info('Using last state.')
                    best_state = self.model.state_dict()
                with swap_state(self.model, best_state):
                    # enhance some samples
                    logger.info('Enhance and save samples...')
                    evaluation_start = time.time()

                    if evaluated_on_test_data:
                        logger.info('Samples already evaluated in cross validation, calculating metrics.')
                        enhanced_dataset = PrHrSet(self.args.samples_dir, enhanced_filenames)
                        enhanced_dataloader = distrib.loader(enhanced_dataset, batch_size=1, shuffle=False, num_workers=self.args.num_workers)
                        lsd, visqol = evaluate_on_saved_data(self.args, enhanced_dataloader, epoch)
                    elif self.args.joint_evaluate_and_enhance:
                        logger.info('Jointly evaluating and enhancing.')
                        lsd, visqol, enhanced_filenames = evaluate(self.args, self.tt_loader, epoch,
                                                              self.model)
                    else: # TODO: fix bug - no spectograms created in enhance function.
                        enhanced_filenames = enhance(self.tt_loader, self.model, self.args)
                        enhanced_dataset = PrHrSet(self.args.samples_dir, enhanced_filenames)
                        enhanced_dataloader = DataLoader(enhanced_dataset, batch_size=1, shuffle=False)
                        lsd, visqol = evaluate_on_saved_data(self.args, enhanced_dataloader, epoch)

                    if epoch == self.epochs - 1 and self.args.log_results:
                        # log results at last epoch
                        if not 'enhanced_dataloader' in locals():
                            enhanced_dataset = PrHrSet(self.args.samples_dir, enhanced_filenames)
                            enhanced_dataloader = DataLoader(enhanced_dataset, batch_size=1, shuffle=False)

                        log_results(self.args, enhanced_dataloader, epoch)


                    logger.info(bold(f'Evaluation Time {time.time() - evaluation_start:.2f}s'))

                metrics.update({METRICS_KEY_LSD: lsd, METRICS_KEY_VISQOL: visqol})



            wandb.log(metrics, step=epoch)
            self.history.append(metrics)
            info = " | ".join(f"{k.capitalize()} {v:.5f}" for k, v in metrics.items())
            logger.info('-' * 70)
            logger.info(bold(f"Overall Summary | Epoch {epoch + 1} | {info}"))

            if distrib.rank == 0:
                json.dump(self.history, open(self.history_file, "w"), indent=2)
                # Save model each epoch
                if self.checkpoint:
                    self._serialize()
                    logger.debug("Checkpoint saved to %s", self.checkpoint_file.resolve())


    def _run_one_epoch(self, epoch, cross_valid=False):
        total_losses = {}
        total_loss = 0
        data_loader = self.tr_loader if not cross_valid else self.cv_loader

        # get a different order for distributed training, otherwise this will get ignored
        data_loader.epoch = epoch

        label = ["Train", "Valid"][cross_valid]
        name = label + f" | Epoch {epoch + 1}"
        logprog = LogProgress(logger, data_loader, updates=self.num_prints, name=name)

        return_spec = 'return_spec' in self.args.experiment and self.args.experiment.return_spec

        for i, data in enumerate(logprog):
            lr, hr = [x.to(self.device) for x in data]

            if return_spec:
                pr_time, pr_spec = self.dmodel(lr, return_spec=return_spec)

                hr_spec = self.dmodel._spec(hr, scale=True)

                hr_reprs = {'time': hr, 'spec': hr_spec}
                pr_reprs = {'time': pr_time, 'spec': pr_spec}
            else:
                pr_time = self.dmodel(lr)

                # logger.info(f'hr shape: {hr.shape}.')
                # logger.info(f'pr shape: {pr_time.shape}.')

                hr_reprs = {'time': hr}
                pr_reprs = {'time': pr_time}

            losses = self._get_losses(hr_reprs, pr_reprs)
            total_generator_loss = 0
            for loss_name, loss in losses['generator'].items():
                total_generator_loss += loss

            # optimize model in training mode
            if not cross_valid:
                self._optimize(total_generator_loss)
                if self.adversarial_mode:
                    self._optimize_adversarial(losses['discriminator'])

            total_loss += total_generator_loss.item()
            for loss_name, loss in losses['generator'].items():
                total_loss_name = 'generator_' + loss_name
                if total_loss_name in total_losses:
                    total_losses[total_loss_name] += loss.item()
                else:
                    total_losses[total_loss_name] = loss.item()

            for loss_name, loss in losses['discriminator'].items():
                total_loss_name = 'discriminator_' + loss_name
                if total_loss_name in total_losses:
                    total_losses[total_loss_name] += loss.item()
                else:
                    total_losses[total_loss_name] = loss.item()

            logprog.update(total_loss=format(total_loss / (i + 1), ".5f"))
            # Just in case, clear some memory
            if return_spec:
                del pr_spec, hr_spec
            del pr_reprs, hr_reprs, pr_time, hr, lr

        avg_losses = {'total': total_loss / (i + 1)}
        avg_losses.update({'evaluation': total_loss / (i + 1)})
        for loss_name, loss in total_losses.items():
            avg_losses.update({loss_name: loss / (i + 1)})

        return avg_losses

    def _get_valid_losses_on_test_data(self, epoch, enhance):
        total_losses = {}
        total_loss = 0
        data_loader = self.tt_loader

        # get a different order for distributed training, otherwise this will get ignored
        data_loader.epoch = epoch

        name = f"Valid | Epoch {epoch + 1}"
        logprog = LogProgress(logger, data_loader, updates=self.num_prints, name=name)

        total_filenames = []

        for i, data in enumerate(logprog):
            (lr, lr_path), (hr, hr_path) = data
            lr = lr.to(self.device)
            hr = hr.to(self.device)

            filename = Path(hr_path[0]).stem
            total_filenames += filename

            hr_spec = self.model._spec(hr, scale=True).detach()
            pr_time, pr_spec, lr_spec = self.dmodel(lr, return_spec=True, return_lr_spec=True)
            pr_spec = pr_spec.detach()
            lr_spec = lr_spec.detach()
            pr_time = match_signal(pr_time, hr.shape[-1])

            if enhance:
                save_wavs(pr_time, lr, hr, [os.path.join(self.args.samples_dir, filename)], self.args.experiment.lr_sr,
                          self.args.experiment.hr_sr)
                save_specs(lr_spec, pr_spec, hr_spec, os.path.join(self.args.samples_dir, filename))

            hr_reprs = {'time': hr, 'spec': hr_spec}
            pr_reprs = {'time': pr_time, 'spec': pr_spec}

            losses = self._get_losses(hr_reprs, pr_reprs)
            total_generator_loss = 0
            for loss_name, loss in losses['generator'].items():
                total_generator_loss += loss

            total_loss += total_generator_loss.item()
            for loss_name, loss in losses['generator'].items():
                total_loss_name = 'generator_' + loss_name
                if total_loss_name in total_losses:
                    total_losses[total_loss_name] += loss.item()
                else:
                    total_losses[total_loss_name] = loss.item()

            for loss_name, loss in losses['discriminator'].items():
                total_loss_name = 'discriminator_' + loss_name
                if total_loss_name in total_losses:
                    total_losses[total_loss_name] += loss.item()
                else:
                    total_losses[total_loss_name] = loss.item()

            logprog.update(total_loss=format(total_loss / (i + 1), ".5f"))
            # Just in case, clear some memory
            del pr_reprs, hr_reprs

        avg_losses = {'total': total_loss / (i + 1)}
        avg_losses.update({'evaluation': total_loss / (i + 1)})
        for loss_name, loss in total_losses.items():
            avg_losses.update({loss_name: loss / (i + 1)})

        return avg_losses, total_filenames if enhance else None


    def _get_losses(self, hr, pr):
        hr_time = hr['time']
        pr_time = pr['time']

        losses = {'generator': {}, 'discriminator': {}}
        with torch.autograd.set_detect_anomaly(True):
            if 'l1' in self.args.losses:
                losses['generator'].update({'l1': F.l1_loss(pr_time, hr_time)})
            if 'l2' in self.args.losses:
                losses['generator'].update({'l2': F.mse_loss(pr_time, hr_time)})
            if 'stft' in self.args.losses:
                stft_loss = self._get_stft_loss(pr_time, hr_time)
                losses['generator'].update({'stft': stft_loss})
            if 'perceptual' in self.args.losses:
                percept_loss = self.perceptual_loss(pr_time.squeeze(dim=1), hr_time.squeeze(dim=1)).mean()
                losses['generator'].update({'perceptual': percept_loss})
            if 'mel' in self.args.losses:
                # L1 Mel-Spectrogram Loss
                hr_mel = self.melspec_transform(hr_time)
                pr_mel = self.melspec_transform(pr_time)
                losses['generator'].update({'mel': F.l1_loss(hr_mel, pr_mel)
                                                   * self.args.experiment.mel_spec_loss_lambda})

            if 'spectral_l1' in self.args.losses:
                losses['generator'].update({'spectral_l1': F.l1_loss(pr['spec'], hr['spec'])})

            if 'spectral_l2' in self.args.losses:
                hr_spec = torch.view_as_real(hr['spec'])
                pr_spec = torch.view_as_real(pr['spec'])
                losses['generator'].update({'spectral_l2': F.mse_loss(pr_spec, hr_spec)})

            if self.adversarial_mode:
                # for the disc_optimizers: necessary to name the adversarial discriminator losses by their
                # respective discriminators
                if self.multiple_discriminators_mode: # this is ugly. Fix when mergning single discriminator case with multiple discriminators case
                    if 'melgan' in self.args.experiment.discriminator_models:
                        generator_losses, discriminator_loss = self._get_melgan_adversarial_loss(pr_time, hr_time)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_melgan': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_melgan': generator_losses['features']})
                        losses['discriminator'].update({'melgan': discriminator_loss})
                    if 'msd' in self.args.experiment.discriminator_models:
                        generator_losses, discriminator_loss = self._get_msd_adversarial_loss(pr_time, hr_time)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_msd': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_msd': generator_losses['features']})
                        losses['discriminator'].update({'msd': discriminator_loss})
                    if 'mpd' in self.args.experiment.discriminator_models:
                        generator_losses, discriminator_loss = self._get_mpd_adversarial_loss(pr_time, hr_time)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_mpd': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_mpd': generator_losses['features']})
                        losses['discriminator'].update({'mpd': discriminator_loss})
                    if 'hifi' in self.args.experiment.discriminator_models:
                        generator_loss, discriminator_loss = self._get_hifi_adversarial_loss(pr_time, hr_time)
                        losses['generator'].update({'adversarial_hifi': generator_loss})
                        losses['discriminator'].update({'hifi': discriminator_loss})
                    if 'mbd' in self.args.experiment.discriminator_models:
                        generator_loss, discriminator_loss = self._get_mbd_adversarial_loss(pr_time, hr_time)
                        losses['generator'].update({'adversarial_mbd': generator_loss})
                        losses['discriminator'].update({'mbd': discriminator_loss})
                    if 'spec' in self.args.experiment.discriminator_models:
                        if self.args.experiment.mel_spec_transform:
                            hr_spec = self.melspec_transform(hr_time).squeeze(dim=1)
                            pr_spec = self.melspec_transform(pr_time).squeeze(dim=1)
                        else:
                            hr_spec = hr['spec'].squeeze(dim=1)
                            pr_spec = pr['spec'].squeeze(dim=1)
                        generator_losses, discriminator_loss = self._get_spec_adversarial_loss(pr_spec, hr_spec)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_spec': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_spec': generator_losses['features']})
                        losses['discriminator'].update({'spec': discriminator_loss})
                    if 'stft' in self.args.experiment.discriminator_models:
                        hr_spec = torch.view_as_real(hr['spec'].squeeze(dim=1)).permute(0, 3, 1, 2)
                        pr_spec = torch.view_as_real(pr['spec'].squeeze(dim=1)).permute(0, 3, 1, 2)
                        generator_loss, discriminator_loss = self._get_stft_adversarial_loss(pr_spec, hr_spec)
                        losses['generator'].update({'adversarial_stft': generator_loss})
                        losses['discriminator'].update({'stft': discriminator_loss})
                else:
                    if self.args.experiment.discriminator_model == 'melgan':
                        generator_losses, discriminator_loss = self._get_melgan_adversarial_loss(pr_time, hr_time)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_melgan': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_melgan': generator_losses['features']})
                        losses['discriminator'].update({'melgan': discriminator_loss})
                    if self.args.experiment.discriminator_model == 'msd':
                        generator_losses, discriminator_loss = self._get_msd_adversarial_loss(pr_time, hr_time)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_msd': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_msd': generator_losses['features']})
                        losses['discriminator'].update({'msd': discriminator_loss})
                    if self.args.experiment.discriminator_model == 'mpd':
                        generator_losses, discriminator_loss = self._get_mpd_adversarial_loss(pr_time, hr_time)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_mpd': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_mpd': generator_losses['features']})
                        losses['discriminator'].update({'mpd': discriminator_loss})
                    if self.args.experiment.discriminator_model == 'hifi':
                        generator_loss, discriminator_loss = self._get_hifi_adversarial_loss(pr_time, hr_time)
                        losses['generator'].update({'adversarial_hifi': generator_loss})
                        losses['discriminator'].update({'hifi': discriminator_loss})
                    if self.args.experiment.discriminator_model == 'mbd':
                        generator_loss, discriminator_loss = self._get_mbd_adversarial_loss(pr_time, hr_time)
                        losses['generator'].update({'adversarial_mbd': generator_loss})
                        losses['discriminator'].update({'mbd': discriminator_loss})
                    if self.args.experiment.discriminator_model == 'spec':
                        if self.args.experiment.mel_spec_transform:
                            hr_spec = self.melspec_transform(hr_time).squeeze(dim=1)
                            pr_spec = self.melspec_transform(pr_time).squeeze(dim=1)
                        else:
                            hr_spec = hr['spec'].squeeze(dim=1)
                            pr_spec = pr['spec'].squeeze(dim=1)
                        generator_losses, discriminator_loss = self._get_spec_adversarial_loss(pr_spec, hr_spec)
                        if not self.args.experiment.only_features_loss:
                            losses['generator'].update({'adversarial_spec': generator_losses['adversarial']})
                        if not self.args.experiment.only_adversarial_loss:
                            losses['generator'].update({'features_spec': generator_losses['features']})
                        losses['discriminator'].update({'spec': discriminator_loss})
                    if self.args.experiment.discriminator_model == 'stft':
                        hr_spec = torch.view_as_real(hr['spec'].squeeze(dim=1)).permute(0, 3, 1, 2)
                        pr_spec = torch.view_as_real(pr['spec'].squeeze(dim=1)).permute(0, 3, 1, 2)
                        generator_loss, discriminator_loss = self._get_stft_adversarial_loss(pr_spec, hr_spec)
                        losses['generator'].update({'adversarial_stft': generator_loss})
                        losses['discriminator'].update({'stft': discriminator_loss})
        return losses

    def _get_stft_loss(self, pr, hr):
        sc_loss, mag_loss = self.mrstftloss(pr.squeeze(1), hr.squeeze(1))
        stft_loss = sc_loss + mag_loss
        return stft_loss

    def _get_melgan_adversarial_loss(self, pr, hr):

        discriminator = self.dmodels['melgan']

        discriminator_fake_detached = discriminator(pr.detach())
        discriminator_real = discriminator(hr)
        discriminator_fake = discriminator(pr)

        total_loss_discriminator = self._get_melgan_discriminator_loss(discriminator_fake_detached, discriminator_real)
        generator_losses = self._get_melgan_generator_loss(discriminator_fake, discriminator_real)


        return generator_losses, total_loss_discriminator


    def _get_melgan_discriminator_loss(self, discriminator_fake, discriminator_real):
        discriminator_loss = 0
        for scale in discriminator_fake:
            discriminator_loss += F.relu(1 + scale[-1]).mean()

        for scale in discriminator_real:
            discriminator_loss += F.relu(1 - scale[-1]).mean()
        return discriminator_loss

    def _get_melgan_generator_loss(self, discriminator_fake, discriminator_real):
        features_loss = 0
        features_weights = 4.0 / (self.args.experiment.melgan_discriminator.n_layers + 1)
        discriminator_weights = 1.0 / self.args.experiment.melgan_discriminator.num_D
        weights = discriminator_weights * features_weights

        for i in range(self.args.experiment.melgan_discriminator.num_D):
            for j in range(len(discriminator_fake[i]) - 1):
                features_loss += weights * F.l1_loss(discriminator_fake[i][j], discriminator_real[i][j].detach())

        adversarial_loss = 0
        for scale in discriminator_fake:
            adversarial_loss += F.relu(1 - scale[-1]).mean()
            # adversarial_loss += -scale[-1].mean()

        if 'only_adversarial_loss' in self.args.experiment and self.args.experiment.only_adversarial_loss:
            return {'adversarial': adversarial_loss}

        if 'only_features_loss' in self.args.experiment and self.args.experiment.only_features_loss:
            return {'features': self.args.experiment.features_loss_lambda * features_loss}

        return {'adversarial': adversarial_loss,
                'features': self.args.experiment.features_loss_lambda * features_loss}


    def _get_hifi_adversarial_loss(self, pr, hr):
        mpd = self.dmodels['mpd']
        msd = self.dmodels['msd']

        # MPD
        y_df_hat_r, y_df_hat_g, _, _ = mpd(hr, pr.detach())
        loss_disc_f = discriminator_loss(y_df_hat_r, y_df_hat_g)

        # MSD
        y_ds_hat_r, y_ds_hat_g, _, _ = msd(hr, pr.detach())
        loss_disc_s = discriminator_loss(y_ds_hat_r, y_ds_hat_g)

        total_loss_discriminator = loss_disc_s + loss_disc_f

        # L1 Mel-Spectrogram Loss
        pr_mel = self.melspec_transform(pr)
        hr_mel = self.melspec_transform(hr)
        loss_mel = F.l1_loss(hr_mel, pr_mel) * self.args.experiment.mel_spec_loss_lambda

        y_df_hat_r, y_df_hat_g, fmap_f_r, fmap_f_g = mpd(hr, pr)
        y_ds_hat_r, y_ds_hat_g, fmap_s_r, fmap_s_g = msd(hr, pr)
        loss_fm_f = feature_loss(fmap_f_r, fmap_f_g)
        loss_fm_s = feature_loss(fmap_s_r, fmap_s_g)
        loss_gen_f = generator_loss(y_df_hat_g)
        loss_gen_s = generator_loss(y_ds_hat_g)

        if 'only_features_loss' in self.args.experiment and self.args.experiment.only_features_loss:
            total_loss_generator = loss_fm_s + loss_fm_f
        else:
            total_loss_generator = loss_gen_s + loss_gen_f + loss_fm_s + loss_fm_f + loss_mel

        return total_loss_generator, total_loss_discriminator


    def _get_msd_adversarial_loss(self, pr, hr):
        msd = self.dmodels['msd']

        # discriminator loss
        y_ds_hat_r, y_ds_hat_g, _, _ = msd(hr, pr.detach())
        d_loss = discriminator_loss(y_ds_hat_r, y_ds_hat_g)

        # generator loss
        y_ds_hat_r, y_ds_hat_g, fmap_s_r, fmap_s_g = msd(hr, pr)
        g_feat_loss = feature_loss(fmap_s_r, fmap_s_g)
        g_adv_loss = generator_loss(y_ds_hat_g)


        if 'only_adversarial_loss' in self.args.experiment and self.args.experiment.only_adversarial_loss:
            return {'adversarial': g_adv_loss}, d_loss

        if 'only_features_loss' in self.args.experiment and self.args.experiment.only_features_loss:
            return {'features': self.args.experiment.features_loss_lambda * g_feat_loss}, d_loss

        return {'adversarial': g_adv_loss,
                'features': self.args.experiment.features_loss_lambda * g_feat_loss}, d_loss


    def _get_mpd_adversarial_loss(self, pr, hr):
        mpd = self.dmodels['mpd']

        # discriminator loss
        y_df_hat_r, y_df_hat_g, _, _ = mpd(hr, pr.detach())
        d_loss = discriminator_loss(y_df_hat_r, y_df_hat_g)

        # generator loss
        y_df_hat_r, y_df_hat_g, fmap_f_r, fmap_f_g = mpd(hr, pr)
        g_feat_loss = feature_loss(fmap_f_r, fmap_f_g)
        g_adv_loss = generator_loss(y_df_hat_g)



        if 'only_adversarial_loss' in self.args.experiment and self.args.experiment.only_adversarial_loss:
            return {'adversarial': g_adv_loss}, d_loss

        if 'only_features_loss' in self.args.experiment and self.args.experiment.only_features_loss:
            return {'features': self.args.experiment.features_loss_lambda * g_feat_loss}, d_loss

        return {'adversarial': g_adv_loss,
                'features': self.args.experiment.features_loss_lambda * g_feat_loss}, d_loss


    def _get_spec_adversarial_loss(self, pr_spec, hr_spec):

        spec_disc = self.dmodels['spec']

        # discriminator loss
        y_ds_hat_r, y_ds_hat_g, _, _ = spec_disc(hr_spec, pr_spec.detach())
        d_loss = discriminator_loss(y_ds_hat_r, y_ds_hat_g)

        # generator loss
        y_ds_hat_r, y_ds_hat_g, fmap_s_r, fmap_s_g = spec_disc(hr_spec, pr_spec)
        g_feat_loss = feature_loss(fmap_s_r, fmap_s_g)
        g_adv_loss = generator_loss(y_ds_hat_g)

        if 'only_adversarial_loss' in self.args.experiment and self.args.experiment.only_adversarial_loss:
            return {'adversarial': g_adv_loss}, d_loss

        if 'only_features_loss' in self.args.experiment and self.args.experiment.only_features_loss:
            return {'features': self.args.experiment.features_loss_lambda * g_feat_loss}, d_loss

        return {'adversarial': g_adv_loss,
                'features': self.args.experiment.features_loss_lambda * g_feat_loss}, d_loss

    def _get_mbd_adversarial_loss(self, pr, hr):
        mbd = self.dmodels['mbd']

        # MBD
        y_ds_hat_r, y_ds_hat_g, _, _ = mbd(hr, pr.detach())
        loss_discriminator = discriminator_loss(y_ds_hat_r, y_ds_hat_g)


        # L1 Mel-Spectrogram Loss
        pr_mel = self.melspec_transform(pr)
        hr_mel = self.melspec_transform(hr)
        loss_mel = F.l1_loss(hr_mel, pr_mel) * self.args.experiment.mel_spec_loss_lambda

        y_ds_hat_r, y_ds_hat_g, fmap_s_r, fmap_s_g = mbd(hr, pr)
        loss_fm_s = feature_loss(fmap_s_r, fmap_s_g)
        loss_gen_s = generator_loss(y_ds_hat_g)
        total_loss_generator = loss_gen_s + loss_fm_s + loss_mel

        return total_loss_generator, loss_discriminator

    def _get_stft_adversarial_loss(self, pr_spec, hr_spec):

        discriminator = self.dmodels['stft_disc']

        discriminator_fake_detached = discriminator(pr_spec.detach().contiguous())
        discriminator_real = discriminator(hr_spec)
        discriminator_fake = discriminator(pr_spec)

        discriminator_loss = self._get_stft_discriminator_loss(discriminator_fake_detached, discriminator_real)
        generator_loss = self._get_stft_generator_loss(discriminator_fake, discriminator_real)

        return generator_loss, discriminator_loss

    def _get_stft_discriminator_loss(self, discriminator_fake, discriminator_real):
        discriminator_loss = F.relu(1 + discriminator_fake[-1]).mean() + F.relu(1 - discriminator_real[-1]).mean()
        return discriminator_loss

    def _get_stft_generator_loss(self, discriminator_fake, discriminator_real):
        generator_loss = 0
        generator_loss += F.relu(1 - discriminator_fake[-1]).mean()

        generator_loss += self.args.experiment.features_loss_lambda * \
                          self.stft_feature_loss(discriminator_real, discriminator_fake)

        return generator_loss

    def stft_feature_loss(self, discriminator_real, discriminator_fake):
        feature_loss = 0
        n_internal_layers = len(discriminator_fake) - 1
        for i in range(n_internal_layers):
            feature_loss += F.l1_loss(discriminator_fake[i], discriminator_real[i].detach())
        return feature_loss / n_internal_layers


    def _optimize(self, loss):
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        # self.scheduler.step()

    def _optimize_adversarial(self, discriminator_losses):
        if self.multiple_discriminators_mode:
            if self.joint_disc_optimizers:
                total_disc_loss = sum(list(discriminator_losses.values()))
                disc_optimizer = self.disc_optimizers['disc_optimizer']
                disc_optimizer.zero_grad()
                total_disc_loss.backward()
                disc_optimizer.step()
            else:
                for discriminator_name, discriminator_loss in discriminator_losses.items():
                    self.disc_optimizers[f'{discriminator_name}'].zero_grad()
                    discriminator_loss.backward()
                    self.disc_optimizers[f'{discriminator_name}'].step()
        else:
            disc_optimizer = self.disc_optimizers['disc_optimizer']
            disc_optimizer.zero_grad()
            list(discriminator_losses.values())[0].backward()
            disc_optimizer.step()