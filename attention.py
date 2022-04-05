import random
import numpy as np 
from collections import OrderedDict
import math
import matplotlib.pyplot as plt 
import pandas as pd 
import scipy.io as sio
import torch
from torch import nn, optim
from torch.autograd import Variable
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_
from dataset import DataSet
import os

device = torch.device("cuda"if torch.cuda.is_available() else "cpu")

'''
    TODO: 
    build seq2seq model
    should be normalized?
'''

class Encoder(nn.Module):
    def __init__(self, input_size, hidden_size,
                 n_layers=1, dropout=0.5):
        super(Encoder, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.cnn_kernel_size = 64
        self.cnn_strides = 8   # when chang 10
        self.cnn = nn.Sequential(
            nn.Conv1d(self.input_size, 64, self.cnn_kernel_size, self.cnn_strides, bias=False),
            nn.PReLU()
            )
        self.cnn_2 = nn.Sequential(
            nn.Conv1d(32,64,self.cnn_kernel_size,self.cnn_strides),
            nn.PReLU()
        )
        self.gru = nn.GRU(64, hidden_size, n_layers,
                          dropout=dropout, bidirectional=True)

    def forward(self, x, len_seq, hidden=None):
        x = x.permute(1,2,0)  # [B*N*T]

        padding = self.cnn_kernel_size - x.size(2) % self.cnn_strides
        x = F.pad(x, (0,padding))
        x = self.cnn(x)

        # padding = self.cnn_kernel_size - x.size(2) % self.cnn_strides
        # x = F.pad(x, (0,padding))
        # x = self.cnn_2(x)

        x = x.permute(2,0,1).contiguous()  # [T*B*N]
        x = nn.utils.rnn.pack_padded_sequence(x,len_seq)
        outputs, hidden = self.gru(x, hidden)
        outputs = nn.utils.rnn.pad_packed_sequence(outputs)
        # pad_packed_sequence return a tuple
        # r_tuple[0] is the padded sequence
        # and r_tuple[1] is a tensor contained length of sequence
        outputs = (outputs[0][:, :, :self.hidden_size] +
                   outputs[0][:, :, self.hidden_size:])
        return outputs, hidden


class Attention(nn.Module):
    def __init__(self, hidden_size):
        super(Attention, self).__init__()
        self.hidden_size = hidden_size
        self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
        self.v = nn.Parameter(torch.rand(hidden_size))
        stdv = 1. / math.sqrt(self.v.size(0))
        self.v.data.uniform_(-stdv, stdv)

    def forward(self, hidden, encoder_outputs, len_seq):
        timestep = encoder_outputs.size(0)
        h = hidden.repeat(timestep, 1,  1).transpose(0, 1)
        encoder_outputs = encoder_outputs.transpose(0, 1)  # [B*T*H]
        attn_energies = self.score(h, encoder_outputs)
        new_atten = []
        for i,l in enumerate(len_seq):
            if l == timestep:
                temp_atten = F.softmax(attn_energies[i:i+1,],dim=1)
            else:
                # temp_atten = torch.cat([F.softmax(attn_energies[i:i+1,:l],dim=1),Variable(torch.zeros(1,timestep-l)).cuda()],1)
                temp_atten = torch.cat([F.softmax(attn_energies[i:i + 1, :l], dim=1), Variable(torch.zeros(1, timestep - l)).to(device)], 1)
            new_atten.append(temp_atten)
        new_atten = torch.cat(new_atten,0)
        return new_atten.unsqueeze(1)

    def score(self, hidden, encoder_outputs):
        # [B*T*2H]->[B*T*H]
        energy = F.relu(self.attn(torch.cat([hidden, encoder_outputs], 2)))
        energy = energy.transpose(1, 2)  # [B*H*T]
        v = self.v.repeat(encoder_outputs.size(0), 1).unsqueeze(1)  # [B*1*H]
        energy = torch.bmm(v, energy)  # [B*1*T]
        return energy.squeeze(1)  # [B*T]


class Decoder(nn.Module):
    def __init__(self, hidden_size, output_size,
                 n_layers=1, dropout=0.2):
        super(Decoder, self).__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers

        self.attention = Attention(hidden_size)
        self.gru = nn.GRU(hidden_size + output_size, hidden_size,
                          n_layers, dropout=dropout)
        self.out = nn.Sequential(
            nn.Linear(hidden_size * 2, output_size)
            )

    def forward(self, input, last_hidden, encoder_outputs, len_seq):
        # Get the embedding of the current input word (last output word)
        # embedded = self.embed(input).unsqueeze(0)  # (1,B,N)
        embedded = input.unsqueeze(0)
        # Calculate attention weights and apply to encoder outputs
        attn_weights = self.attention(last_hidden[-1], encoder_outputs, len_seq)
        context = attn_weights.bmm(encoder_outputs.transpose(0, 1))  # (B,1,N)
        context = context.transpose(0, 1)  # (1,B,N)
        # Combine embedded input word and attended context, run through RNN
        rnn_input = torch.cat([embedded, context], 2)
        output, hidden = self.gru(rnn_input, last_hidden)
        output = output.squeeze(0)  # (1,B,N) -> (B,N)
        context = context.squeeze(0)
        output = self.out(torch.cat([output, context], 1))
        # output = F.log_softmax(output, dim=1)
        return output, hidden, attn_weights


class Seq2Seq(nn.Module):
    def __init__(self, encoder, decoder, teacher_forcing_ratio=0.5):
        super(Seq2Seq, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.teacher_forcing_ratio = teacher_forcing_ratio

    def forward(self, src, trg, len_seq, teacher_forcing_ratio=None, is_analyse=False):
        batch_size = trg.size(1)
        max_len = trg.size(0)
        vocab_size = self.decoder.output_size
        # outputs = Variable(torch.zeros(max_len, batch_size, vocab_size)).cuda()
        outputs = Variable(torch.zeros(max_len, batch_size, vocab_size)).to(device)

        encoder_output, hidden = self.encoder(src, len_seq)
        hidden = hidden[:self.decoder.n_layers]
        output = Variable(trg.data[0,])  # sos

        if is_analyse:
            analyse_data = OrderedDict()
            analyse_data['fea_after_encoder'] = encoder_output.data.cpu().numpy()
            analyse_data['atten'] = []
        for t in range(1, max_len):
            output, hidden, attn_weights = self.decoder(
                    output, hidden, encoder_output, len_seq)
            outputs[t] = output
            if teacher_forcing_ratio == None:
                teacher_forcing_ratio = self.teacher_forcing_ratio
            is_teacher = random.random() < teacher_forcing_ratio
            # output = Variable(trg.data[t,] if is_teacher else output).cuda()
            output = Variable(trg.data[t,] if is_teacher else output).to(device)
            if is_analyse:
                analyse_data['atten'].append(attn_weights.data.cpu().numpy())
        if is_analyse:
            analyse_data['atten'] = np.concatenate(analyse_data['atten'],axis=0)
            return outputs, analyse_data
        else:
            return outputs


class RUL():
    def __init__(self):
        self.hidden_size = 200
        self.epochs = 500
        self.lr = 1e-3
        self.gama = 0.7
        self.dataset = DataSet.load_dataset(name='phm_data')
        self.train_bearings = ['Bearing1_1','Bearing1_2','Bearing2_1','Bearing2_2','Bearing3_1','Bearing3_2']
        self.test_bearings = ['Bearing1_3','Bearing1_4','Bearing1_5','Bearing1_6','Bearing1_7',
                                'Bearing2_3','Bearing2_4','Bearing2_5','Bearing2_6','Bearing2_7',
                                'Bearing3_3']

    
    def train(self):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        train_data,train_label = self._preprocess('train')
        train_iter = [[train_data[i],train_label[i]] for i in range(len(train_data))]
        test_data,test_label = self._preprocess('test')
        val_iter = [[test_data[i],test_label[i]] for i in range(len(test_data))]

        encoder = Encoder(self.feature_size,self.hidden_size,n_layers=2,dropout=0.5)
        decoder = Decoder(self.hidden_size,1,n_layers=2,dropout=0.5)
        # seq2seq = Seq2Seq(encoder,decoder).cuda()
        seq2seq = Seq2Seq(encoder, decoder).to(device)
        # seq2seq = torch.load('./model/newest_seq2seq')
        seq2seq.teacher_forcing_ratio = 0.3
        optimizer = optim.Adam(seq2seq.parameters(), lr=self.lr)
        # optimizer = optim.RMSprop(seq2seq.parameters(), lr=self.lr)
        # optimizer = optim.SGD(seq2seq.parameters(), lr=self.lr, momentum=0.5)
        # optimizer = optim.LBFGS(seq2seq.parameters(),lr=self.lr)

        log = OrderedDict()
        log['train_loss'] = []
        log['val_loss'] = []
        log['test_loss'] = []
        log['teacher_ratio'] = []
        count = 0
        count2 = 0
        e0 = 15
        for e in range(1, self.epochs+1):
            train_loss = self._fit(e, seq2seq, optimizer, train_iter)
            val_loss = self._evaluate(seq2seq, train_iter)
            test_loss = self._evaluate(seq2seq, val_iter)
            print("[Epoch:%d][train_loss:%.4e][val_loss:%.4e][test_loss:%.4e][teacher_ratio:%.4f] "
                % (e, train_loss, val_loss, test_loss, seq2seq.teacher_forcing_ratio))
            log['train_loss'].append(float(train_loss))
            log['val_loss'].append(float(val_loss))
            log['test_loss'].append(float(test_loss))
            log['teacher_ratio'].append(seq2seq.teacher_forcing_ratio)

            '''
            pd.DataFrame(log).to_csv('./model/log.csv',index=False)
            '''
            filename = './model/log.csv'
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, 'wb') as f:
                pd.DataFrame(log).to_csv('./model/log.csv',index=False)

            if float(val_loss) == min(log['val_loss']):
                torch.save(seq2seq, './model/seq2seq')
            torch.save(seq2seq, './model/newest_seq2seq')

            count2 += 1
            if float(train_loss) <= float(val_loss)*0.2:
                count += 1
            else:
                count = 0
            if count >= 3 or count2 >= 50:
                seq2seq.teacher_forcing_ratio = max(1e-5,self.gama*seq2seq.teacher_forcing_ratio)
                count -= 1
                count2 = 0

            # optimizer.param_groups[0]['lr'] = (self.lr - (e%e0) * (self.lr-1e-7) / e0)*0.99**e

    def test(self):
        train_data,train_label = self._preprocess('train')
        train_iter = [[train_data[i],train_label[i]] for i in range(len(train_data))]
        test_data,test_label = self._preprocess('test')
        val_iter = [[test_data[i],test_label[i]] for i in range(len(test_data))]

        seq2seq = torch.load('./model/seq2seq')
        self._plot_result(seq2seq, train_iter, val_iter)

    def analyse(self):
        analyse_data = OrderedDict()
        train_data, train_data_no_norm, train_label = self._preprocess('train',is_analyse=True)
        train_iter = [[train_data[i],train_label[i]] for i in range(len(train_data))]
        test_data, test_data_no_norm, test_label = self._preprocess('test',is_analyse=True)
        val_iter = [[test_data[i],test_label[i]] for i in range(len(test_data))]

        analyse_data['train_data'] = train_data
        analyse_data['train_data_no_norm'] = train_data_no_norm
        analyse_data['train_label'] = train_label
        analyse_data['test_data'] = test_data
        analyse_data['test_data_no_norm'] = test_data_no_norm
        analyse_data['test_label'] = test_label

        seq2seq = torch.load('./model/seq2seq')
        seq2seq.eval()

        analyse_data['train_fea_after_encoder'] = []
        analyse_data['train_atten'] = []
        analyse_data['train_result'] = []

        with torch.no_grad():
            for [data, label] in train_iter:
                data, label = torch.from_numpy(data.copy()), torch.from_numpy(label.copy())
                data, label = data.type(torch.FloatTensor), label.type(torch.FloatTensor)
                # data = Variable(data).cuda()
                data = Variable(data).to(device)
                # label = Variable(label).cuda()
                label = Variable(label).to(device)
                # output, temp_analyse_data = seq2seq(data, label, teacher_forcing_ratio=0.0, is_analyse=True)
                output, temp_analyse_data = seq2seq(data, label, len_seq=[label.size(0)], teacher_forcing_ratio=0.0, is_analyse=True)
                analyse_data['train_result'].append(output.data.cpu().numpy())
                analyse_data['train_fea_after_encoder'].append(temp_analyse_data['fea_after_encoder'])
                analyse_data['train_atten'].append(temp_analyse_data['atten'])

        analyse_data['test_fea_after_encoder'] = []
        analyse_data['test_atten'] = []
        analyse_data['test_result'] = []

        with torch.no_grad():
            for [data, label] in val_iter:
                data, label = torch.from_numpy(data.copy()), torch.from_numpy(label.copy())
                data, label = data.type(torch.FloatTensor), label.type(torch.FloatTensor)
                # data = Variable(data).cuda()
                data = Variable(data).to(device)
                # label = Variable(label).cuda()
                label = Variable(label).to(device)
                output, temp_analyse_data = seq2seq(data, label, len_seq=[label.size(0)], teacher_forcing_ratio=0.0, is_analyse=True)
                analyse_data['test_result'].append(output.data.cpu().numpy())
                analyse_data['test_fea_after_encoder'].append(temp_analyse_data['fea_after_encoder'])
                analyse_data['test_atten'].append(temp_analyse_data['atten'])

        sio.savemat('analyse_data.mat',analyse_data)

    def _custom_loss(self, pred, tru, seq_len):
        total_loss = 0
        for i,l in enumerate(seq_len):
            loss = torch.mean((tru[:l,i,:] - pred[:l,i,:])**2)
            total_loss += loss
        return total_loss/len(seq_len)

        
    def _evaluate(self, model, val_iter):
        model.eval()
        total_loss = 0
        for [data, label] in val_iter:
            with torch.no_grad():
                data, label = torch.from_numpy(data.copy()), torch.from_numpy(label.copy())
                data, label = data.type(torch.FloatTensor), label.type(torch.FloatTensor)
                # data = Variable(data).cuda()
                data = Variable(data).to(device)
                # label = Variable(label).cuda()
                label = Variable(label).to(device)
                output = model(data, label, len_seq=[label.size(0)], teacher_forcing_ratio=0.0)
            loss = F.mse_loss(output,label)
            total_loss += loss.data
        return total_loss / len(val_iter)


    def _fit(self, e, model, optimizer, train_iter, grad_clip=10.0):
        model.train()
        batchbyn = 1
        max_data_len = 0
        max_label_len = 0
        random.shuffle(train_iter)
        train_data = []
        train_label = []
        seq_label_len = []
        for [data, label] in train_iter:
            for _ in range(batchbyn):
                random_idx = random.randint(0,round(label.shape[0]*0.3))
                train_data.append(data[random_idx*8:,]) # when chang 10
                train_label.append(label[random_idx:,])
                max_data_len = max(max_data_len,data.shape[0]-random_idx*8) # when chang 10
                max_label_len = max(max_label_len,label.shape[0]-random_idx)
                seq_label_len.append(label.shape[0]-random_idx)
        
        for i,data in enumerate(train_data):
            train_data[i] = np.concatenate((data,np.zeros((max_data_len-data.shape[0],1,self.feature_size))),axis=0)
        for i,label in enumerate(train_label):
            train_label[i] = np.concatenate((label,-np.ones((max_label_len-label.shape[0],1,1))),axis=0)
        train_data = np.concatenate(train_data,axis=1)
        train_label = np.concatenate(train_label,axis=1)

        sorted_len_seq_t = sorted(enumerate(seq_label_len), key=lambda x:x[1],reverse=True)
        sorted_len_seq = [x for (_,x) in sorted_len_seq_t]
        sorted_index = [x for (x,_) in sorted_len_seq_t]
        train_data = train_data[:,sorted_index,:]
        train_label = train_label[:,sorted_index,:]

        train_data, train_label = torch.from_numpy(train_data), torch.from_numpy(train_label)
        train_data, train_label = train_data.type(torch.FloatTensor), train_label.type(torch.FloatTensor)
        # train_data, train_label = Variable(train_data).cuda(), Variable(train_label).cuda()
        train_data, train_label = Variable(train_data).to(device), Variable(train_label).to(device)
        optimizer.zero_grad()
        output = model(train_data, train_label, sorted_len_seq)
        # loss = F.mse_loss(output, train_label)
        loss = self._custom_loss(output,train_label,sorted_len_seq)
        loss.backward()
        # clip_grad_norm_(model.parameters(), grad_clip)
        optimizer.step()
        return loss.data


    def _plot_result(self, model, train_iter, val_iter):
        model.eval()

        labels = []
        outputs = []
        with torch.no_grad():
            for [data, label] in train_iter:
                data, label = torch.from_numpy(data.copy()), torch.from_numpy(label.copy())
                data, label = data.type(torch.FloatTensor), label.type(torch.FloatTensor)
                # data = Variable(data).cuda()
                data = Variable(data).to(device)
                # label = Variable(label).cuda()
                label = Variable(label).to(device)
                # output = model(data, label,  teacher_forcing_ratio=0.0)
                output = model(data, label, len_seq=[label.size(0)], teacher_forcing_ratio=0.0)
                labels.append(label.data.cpu().numpy())
                outputs.append(output.data.cpu().numpy())
        labels = np.concatenate(tuple(x for x in labels), axis=0)
        outputs = np.concatenate(tuple(x for x in outputs), axis=0)
        labels, outputs = labels.reshape(-1,), outputs.reshape(-1,)
        plt.subplot(2,1,1)
        plt.plot(labels)
        plt.plot(outputs)

        labels = []
        outputs = []
        with torch.no_grad():
            for [data, label] in val_iter:
                data, label = torch.from_numpy(data.copy()), torch.from_numpy(label.copy())
                data, label = data.type(torch.FloatTensor), label.type(torch.FloatTensor)
                # data = Variable(data).cuda()
                data = Variable(data).to(device)
                # label = Variable(label).cuda()
                label = Variable(label).to(device)
                # output = model(data, label, teacher_forcing_ratio=0.0)
                output = model(data, label, len_seq=[label.size(0)], teacher_forcing_ratio=0.0)
                labels.append(label.data.cpu().numpy())
                outputs.append(output.data.cpu().numpy())
        labels = np.concatenate(tuple(x for x in labels), axis=0)
        outputs = np.concatenate(tuple(x for x in outputs), axis=0)
        labels, outputs = labels.reshape(-1,), outputs.reshape(-1,)
        plt.subplot(2,1,2)
        plt.plot(labels)
        plt.plot(outputs)
        plt.show()

    
    def _preprocess(self, select, is_analyse=False):
        if select == 'train':
            temp_data = self.dataset.get_value('data',condition={'bearing_name':self.train_bearings})
            temp_label = self.dataset.get_value('RUL',condition={'bearing_name':self.train_bearings})
        elif select == 'test':
            temp_data = self.dataset.get_value('data',condition={'bearing_name':self.test_bearings})
            temp_label = self.dataset.get_value('RUL',condition={'bearing_name':self.test_bearings})
        else:
            raise ValueError('wrong selection!')

        for i,x in enumerate(temp_label):
            temp_label[i] = np.arange(temp_data[i].shape[0]) + x
            temp_label[i] = temp_label[i][:,np.newaxis,np.newaxis]
            temp_label[i] = temp_label[i] / np.max(temp_label[i])
            temp_label[i] = temp_label[i][::8] # when chang 10
        for i,x in enumerate(temp_data):
            temp_data[i] = x[::-1,].transpose(0,2,1)
        time_feature = [self._get_time_fea(x) for x in temp_data]
        if is_analyse:
            time_feature_no_norm = [self._get_time_fea(x, is_norm=False) for x in temp_data]
            return time_feature, time_feature_no_norm, temp_label
        else:
            return time_feature, temp_label

    def _get_time_fea(self, data, is_norm=True):
        fea_dict = OrderedDict()
        fea_dict['mean'] = np.mean(data,axis=2,keepdims=True)
        fea_dict['rms'] = np.sqrt(np.mean(data**2,axis=2,keepdims=True))
        fea_dict['kur'] = np.sum((data-fea_dict['mean'].repeat(data.shape[2],axis=2))**4,axis=2) \
                / (np.var(data,axis=2)**2*data.shape[2])
        fea_dict['kur'] = fea_dict['kur'][:,:,np.newaxis]
        fea_dict['skew'] = np.sum((data-fea_dict['mean'].repeat(data.shape[2],axis=2))**3,axis=2) \
                / (np.var(data,axis=2)**(3/2)*data.shape[2])
        fea_dict['skew'] = fea_dict['skew'][:,:,np.newaxis]
        fea_dict['p2p'] = np.max(data,axis=2,keepdims=True) - np.min(data,axis=2,keepdims=True)
        fea_dict['var'] = np.var(data,axis=2,keepdims=True)
        fea_dict['cre'] = np.max(abs(data),axis=2,keepdims=True) / fea_dict['rms']
        fea_dict['imp'] = np.max(abs(data),axis=2,keepdims=True) \
                / np.mean(abs(data),axis=2,keepdims=True)
        fea_dict['mar'] = np.max(abs(data),axis=2,keepdims=True) \
                / (np.mean((abs(data))**0.5,axis=2,keepdims=True))**2
        fea_dict['sha'] = fea_dict['rms'] / np.mean(abs(data),axis=2,keepdims=True)
        fea_dict['smr'] = (np.mean((abs(data))**0.5,axis=2,keepdims=True))**2
        fea_dict['cle'] = fea_dict['p2p'] / fea_dict['smr']

        fea = np.concatenate(tuple(x for x in fea_dict.values()),axis=2)
        fea = fea.reshape(-1,fea.shape[1]*fea.shape[2])
        self.feature_size = fea.shape[1]
        if is_norm:
            fea = self._normalize(fea,dim=1)
        fea = fea[:,np.newaxis,:]
        return fea
    
    def _get_fre_fea(self, data):
        pass

    def _normalize(self, data, dim=None):
        if dim == None:
            mmrange = 10.**np.ceil(np.log10(np.max(data) - np.min(data)))
            r_data = (data - np.min(data)) / mmrange
        else:
            mmrange = 10.**np.ceil(np.log10(np.max(data,axis=dim,keepdims=True) - np.min(data,axis=dim,keepdims=True)))
            # mmrange = np.max(data,axis=dim,keepdims=True) - np.min(data,axis=dim,keepdims=True)
            r_data = (data - np.min(data,axis=dim,keepdims=True).repeat(data.shape[dim],axis=dim)) \
                / (mmrange).repeat(data.shape[dim],axis=dim)
        return r_data


if __name__ == '__main__':
    process = RUL()
    process.train()
    process.analyse()
    process.test()
