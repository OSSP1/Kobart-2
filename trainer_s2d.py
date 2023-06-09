
import argparse
import os
import glob
import torch
import ast
import numpy as np
import pandas as pd
from tqdm import tqdm, trange
from torch.utils.data import Dataset, DataLoader, IterableDataset
import logging
import pytorch_lightning as pl
from pytorch_lightning import loggers as pl_loggers
from transformers import BartForConditionalGeneration, BartTokenizer, PreTrainedTokenizerFast, BartModel
from transformers.optimization import AdamW, get_cosine_schedule_with_warmup
from kobart import get_pytorch_kobart_model, get_kobart_tokenizer
# from torchtext.data.metrics import bleu_score

# batch_size
bs = 64
# max_epoch 
ep = 10


class DialectDataset(Dataset):
    def __init__(self, file, tokenizer, max_len, pad_index = 0, ignore_index=-100):
        super().__init__()
        self.tokenizer = tokenizer
        self.max_len = max_len
        self.docs = pd.read_csv(file, sep='\t', lineterminator='\n')
        self.len = len(self.docs)
        self.pad_index = pad_index
        self.ignore_index = ignore_index

    def add_padding_data(self, inputs):
        if len(inputs) < self.max_len:
            pad = np.array([self.pad_index] *(self.max_len - len(inputs)))
            inputs = np.concatenate([inputs, pad])
        else:
            inputs = inputs[:self.max_len]

        return inputs

    def add_ignored_data(self, inputs):
        if len(inputs) < self.max_len:
            pad = np.array([self.ignore_index] *(self.max_len - len(inputs)))
            inputs = np.concatenate([inputs, pad])
        else:
            inputs = inputs[:self.max_len]

        return inputs
    
    def __getitem__(self, idx):
        instance = self.docs.iloc[idx]
        input_ids = self.tokenizer.encode(instance['standard'])
        input_ids = self.add_padding_data(input_ids)
        
        label_ids = self.tokenizer.encode(instance['dialect'])
        label_ids.append(self.tokenizer.eos_token_id)
        dec_input_ids = [self.pad_index]
        dec_input_ids += label_ids[:-1]
        dec_input_ids = self.add_padding_data(dec_input_ids)
        label_ids = self.add_ignored_data(label_ids)

        # return (torch.tensor(input_ids, dtype=torch.long),
        #         torch.tensor(dec_input_ids, dtype=torch.long),
        #         torch.tensor(label_ids, dtype=torch.long))
        return {'input_ids': torch.tensor(input_ids, dtype=torch.long),
                'decoder_input_ids': torch.tensor(dec_input_ids, dtype=torch.long),
                'labels': torch.tensor(label_ids, dtype=torch.long)}
    
    def __len__(self):
        return self.len

class DialectDataModule(pl.LightningDataModule):
    def __init__(self, train_file,
                 test_file, tokenizer,
                 max_len=128,
                 batch_size=bs,
                 num_workers=4):
        super().__init__()
        self.batch_size = batch_size
        self.max_len = max_len
        self.train_file_path = train_file
        self.test_file_path = test_file
        self.tokenizer = tokenizer
        self.num_workers = num_workers

    def setup(self, stage):
        # split dataset
        self.train = DialectDataset(self.train_file_path,
                                 self.tokenizer,
                                 self.max_len)
        self.test = DialectDataset(self.test_file_path,
                                self.tokenizer,
                                self.max_len)

    def train_dataloader(self):
        train = DataLoader(self.train,
                           batch_size=self.batch_size,
                           num_workers=self.num_workers, shuffle=True)
        return train

    def val_dataloader(self):
        val = DataLoader(self.test,
                         batch_size=self.batch_size,
                         num_workers=self.num_workers, shuffle=False)
        return val

    def test_dataloader(self):
        test = DataLoader(self.test,
                          batch_size=self.batch_size,
                          num_workers=self.num_workers, shuffle=False)
        return test

class Base(pl.LightningModule):
    def __init__(self):
        super(Base, self).__init__()
        

    def configure_optimizers(self):
        # Prepare optimizer
        param_optimizer = list(self.model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(
                nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(
                nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=2e-7, correct_bias=False)
        # warm up lr
        num_workers = 4
        data_len = len(data_module.train)
        logging.info(f'number of workers {num_workers}, data length {data_len}')
        num_train_steps = int(data_len / (bs * num_workers) * ep) # batch size, max epochs
        logging.info(f'num_train_steps : {num_train_steps}')
        num_warmup_steps = int(num_train_steps * 0.1) # warm up ratio
        logging.info(f'num_warmup_steps : {num_warmup_steps}')
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=num_warmup_steps, num_training_steps=num_train_steps)
        lr_scheduler = {'scheduler': scheduler, 
                        'monitor': 'loss', 'interval': 'step',
                        'frequency': 1}
        return [optimizer], [lr_scheduler]


class DialectConvertor(Base):
    def __init__(self):
        super().__init__()
        self.tokenizer = tokenizer
        self.model = BartForConditionalGeneration.from_pretrained(get_pytorch_kobart_model())
        self.model.resize_token_embeddings(len(tokenizer))
        self.model.train()
        self.bos_token = '<s>'
        self.eos_token = '</s>'
        self.pad_token_id = 0

    def forward(self, inputs):

        attention_mask = inputs['input_ids'].ne(self.pad_token_id).float()
        
        decoder_attention_mask = inputs['decoder_input_ids'].ne(self.pad_token_id).float()
    

        return self.model(input_ids=inputs['input_ids'],
                          attention_mask=attention_mask,
                          decoder_input_ids=inputs['decoder_input_ids'],
                          decoder_attention_mask=decoder_attention_mask,
                          labels=inputs['labels'], return_dict=True)


    def training_step(self, batch, batch_idx):
        outs = self(batch)
        loss = outs.loss
        self.log('train_loss', loss, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        outs = self(batch)
        loss = outs['loss']
        return (loss)

    def validation_epoch_end(self, outputs):
        losses = []
        for loss in outputs:
            losses.append(loss)
        self.log('val_loss', torch.stack(losses).mean(), prog_bar=True)


if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description='Dialect Machine Translation')

    args = parser.parse_args()
    
    tokenizer = get_kobart_tokenizer()

    data_module=DialectDataModule('data/train_cleaned.tsv',
                              'data/test_cleaned.tsv', tokenizer)
    
    model = DialectConvertor()
    
    checkpoint_callback = pl.callbacks.ModelCheckpoint(monitor='val_loss',
                                                       dirpath='model_results/s2d/jeju_tranlation',
                                                       filename='model_chp/{epoch:02d}-{val_loss:.3f}',
                                                       verbose=True,
                                                       save_last=True,
                                                       mode='min',
                                                       save_top_k=-1
                                                    #    prefix='jeju_translation'
                                                       )
    
    lr_logger = pl.callbacks.LearningRateMonitor()
    tb_logger = pl_loggers.TensorBoardLogger(os.path.join("D:/OSSP_model_ckpt/jeju_translation", 'tb_logs'))
    trainer = pl.Trainer(logger=tb_logger, callbacks=[checkpoint_callback, lr_logger],
                    max_epochs=3, gpus=1, #progress_bar_refresh_rate=30,accelerator="dp"
                    )
    
    trainer.fit(model, data_module)
    
    model.model.save_pretrained('model_results/s2d/jeju_tranlation/')
