import torch, torch.nn as nn, torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
import math
class SimpleAttention(nn.Module):    
    def __init__(self, input_dim):
        super().__init__()
        self.input_dim = input_dim
        self.scalar = nn.Linear(self.input_dim,1,bias=False)
    
    def forward(self, M):
        scale = self.scalar(M)
        alpha = F.softmax(scale, dim=0).permute(1,2,0)
        attn_pool = torch.bmm(alpha, M.transpose(0,1))[:,0,:]
        return attn_pool, alpha

class BiModalAttention(nn.Module):
    def __init__(self, mode1, mode2):
        super().__init__()
        self.mode1 = mode1.permute(1, 0, 2)
        self.mode2 = mode2.permute(1, 0, 2)
        
    def forward(self):
        c1, c2 = torch.bmm(self.mode1, self.mode2.transpose(1, 2)), torch.bmm(self.mode2, self.mode1.transpose(1, 2))     
        n1, n2 = F.softmax(c1, dim=-1), F.softmax(c2, dim=-1)
        o1, o2 = torch.bmm(n1, self.mode2), torch.bmm(n2, self.mode1)
        a1, a2 = o1 * self.mode1, o2 * self.mode2
        return torch.cat([a1, a2], dim=-1).permute(1, 0, 2)
    
class MultilogueNetCell(nn.Module):
    def __init__(self, D_m_party, D_m_context, D_g, D_p , D_e, D_a =100, dropout=0.5):
        super().__init__()
        self.D_m_party, self.D_m_context = D_m_party, D_m_context
        self.D_g, self.D_p, self.D_e = D_g, D_p, D_e
        self.g_cell = nn.GRUCell(D_m_context + D_p, D_g)
        self.p_cell = nn.GRUCell(D_m_party + D_g, D_p)
        self.e_cell = nn.GRUCell(D_p, D_e)
        self.dropout = nn.Dropout(dropout)
        self.attention = SimpleAttention(D_g)

    def _select_parties(self, X, indices):
        q0_sel = []
        for idx, j in zip(indices, X):
            q0_sel.append(j[idx].unsqueeze(0))
        q0_sel = torch.cat(q0_sel,0)
        return q0_sel

    def forward(self, U, U_context, qmask, g_hist, q0, e0):
        qm_idx = torch.argmax(qmask, 1)
        q0_sel = self._select_parties(q0, qm_idx)
        g_ = self.g_cell(torch.cat([U_context,q0_sel], dim=1),
                torch.zeros(U_context.size()[0],self.D_g).type(U.type()) if g_hist.size()[0]==0 else
                g_hist[-1])
        g_ = self.dropout(g_)
        if g_hist.size()[0]==0:
            c_ = torch.zeros(U_context.size()[0],self.D_g).type(U.type())
            alpha = None
        else:
            c_, alpha = self.attention(g_hist)
        U_c_ = torch.cat([U,c_], dim=1).unsqueeze(1).expand(-1, qmask.size()[1],-1)
        qs_ = self.p_cell(U_c_.contiguous().view(-1, self.D_m_party+self.D_g),q0.view(-1, self.D_p)).view(U.size()[0],-1,self.D_p)
        qs_ = self.dropout(qs_)
        ql_ = q0
        qmask_ = qmask.unsqueeze(2)
        q_ = ql_*(1-qmask_) + qs_*qmask_
        e0 = torch.zeros(qmask.size()[0], self.D_e).type(U.type()) if e0.size()[0]==0\
                else e0
        e_ = self.e_cell(self._select_parties(q_,qm_idx), e0)
        e_ = self.dropout(e_)
        return g_,q_,e_,alpha

class MultilogueNet(nn.Module):
    def __init__(self, D_m_party, D_m_context, D_g, D_p, D_e, D_a=100, dropout=0.5):
        super().__init__()
        self.D_m_party, self.D_m_context = D_m_party, D_m_context
        self.D_g, self.D_p, self.D_e = D_g, D_p, D_e
        self.dropout = nn.Dropout(dropout)
        self.multilogue_cell = MultilogueNetCell(D_m_party, D_m_context, D_g, D_p, D_e, D_a, dropout)

    def forward(self, U, U_context, qmask):
        g_hist = torch.zeros(0).type(U_context.type()) 
        q_ = torch.zeros(qmask.size()[1], qmask.size()[2],
                                    self.D_p).type(U.type())
        e_ = torch.zeros(0).type(U.type()) 
        e = e_
        alpha = []
        for u_,uc_,qmask_ in zip(U, U_context, qmask):
            g_, q_, e_, alpha_ = self.multilogue_cell(u_, uc_, qmask_, g_hist, q_, e_)
            g_hist = torch.cat([g_hist, g_.unsqueeze(0)],0)
            e = torch.cat([e, e_.unsqueeze(0)],0)
            if type(alpha_)!=type(None):
                alpha.append(alpha_[:,0,:])
        return e,alpha 
# can select a subset of (visual, audio, text) modality
class RegressionModel_(nn.Module):       
    def __init__(self, D_m_party_text,D_m_party_audio,D_m_party_video, D_m_context, D_g, D_p, D_e, D_h, D_a=100, dropout_rec=0.5, dropout=0.5, active_modal=[True, True, True]):
        super().__init__()
        self.D_m_party_text = D_m_party_text
        self.D_m_party_audio = D_m_party_audio
        self.D_m_party_video = D_m_party_video
        self.D_m_context = D_m_context
        self.D_g       = D_g
        self.D_p       = D_p
        self.D_e       = D_e
        self.D_h       = D_h
        self.dropout   = nn.Dropout(dropout)
        self.dropout_rec = nn.Dropout(dropout)
        self.multilogue_net_f_t = MultilogueNet(D_m_party_text, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_r_t = MultilogueNet(D_m_party_text, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_f_a = MultilogueNet(D_m_party_audio, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_r_a = MultilogueNet(D_m_party_audio, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_f_v = MultilogueNet(D_m_party_video, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_r_v = MultilogueNet(D_m_party_video, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.linear     = nn.Linear(D_e, D_h)
        hid_num = sum(active_modal) + 2 * self._nCr(sum(active_modal), 2)
        self.smax_fc    = nn.Linear(hid_num*D_h, 1)
        self.active_modal = active_modal     # text / video / audio

    def _reverse_seq(self, X, mask):
        X_ = X.transpose(0,1)
        mask_sum = torch.sum(mask, 1).int()
        xfs = []
        for x, c in zip(X_, mask_sum):
            xf = torch.flip(x[:c], [0])
            xfs.append(xf)
        return pad_sequence(xfs)

    def _nCr(self, n, r):
        f = math.factorial
        if n < r: return 0
        return int(f(n) / f(r) / f(n-r))
    
    def forward(self, U_t=None, U_a=None, U_v=None, U_context=None, qmask=None, umask=None):
        bi_hid = []
        hid = []
        active_num = sum(self.active_modal)

        # text
        if self.active_modal[0]:
            emotions_f_t,_ = self.multilogue_net_f_t(U_t, U_context, qmask)
            emotions_f_t = self.dropout_rec(emotions_f_t)
            hidden_t = (self.linear(emotions_f_t))
            hidden_t = (self.dropout(hidden_t))
            hid.append(hidden_t)
        
        # video
        if self.active_modal[1]:
            emotions_f_v,_ = self.multilogue_net_f_v(U_v, U_context, qmask)                        
            emotions_f_v = self.dropout_rec(emotions_f_v)
            hidden_v = (self.linear(emotions_f_v))
            hidden_v = (self.dropout(hidden_v))
            hid.append(hidden_v)

        # audio
        if self.active_modal[2]:
            emotions_f_a,_ = self.multilogue_net_f_a(U_a, U_context, qmask)
            emotions_f_a = self.dropout_rec(emotions_f_a)
            hidden_a = (self.linear(emotions_f_a))
            hidden_a = (self.dropout(hidden_a))
            hid.append(hidden_a)
        
        if self.active_modal[0] and self.active_modal[2]:
            hidden_at = BiModalAttention(hidden_t, hidden_a) 
            bi_hid.append(hidden_at())
        if self.active_modal[1] and self.active_modal[2]:
            hidden_va = BiModalAttention(hidden_v, hidden_a)  
            bi_hid.append(hidden_va())
        if self.active_modal[0] and self.active_modal[1]:
            hidden_tv = BiModalAttention(hidden_t, hidden_v)  
            bi_hid.append(hidden_tv())
        # print(torch.cat(bi_hid + hid,dim=-1).shape)
        pred = active_num * torch.tanh(self.smax_fc(torch.cat(bi_hid + hid,dim=-1)).squeeze()) 
        return pred.transpose(0,1).contiguous().view(-1)

class RegressionModel(nn.Module):       
    def __init__(self, D_m_party_text,D_m_party_audio,D_m_party_video, D_m_context, D_g, D_p, D_e, D_h, D_a=100, dropout_rec=0.5, dropout=0.5):
        super().__init__()
        self.D_m_party_text = D_m_party_text
        self.D_m_party_audio = D_m_party_audio
        self.D_m_party_video = D_m_party_video
        self.D_m_context = D_m_context
        self.D_g       = D_g
        self.D_p       = D_p
        self.D_e       = D_e
        self.D_h       = D_h
        self.dropout   = nn.Dropout(dropout)
        self.dropout_rec = nn.Dropout(dropout)
        self.multilogue_net_f_t = MultilogueNet(D_m_party_text, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_r_t = MultilogueNet(D_m_party_text, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_f_a = MultilogueNet(D_m_party_audio, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_r_a = MultilogueNet(D_m_party_audio, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_f_v = MultilogueNet(D_m_party_video, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.multilogue_net_r_v = MultilogueNet(D_m_party_video, D_m_context, D_g, D_p, D_e, D_a, dropout_rec)
        self.linear     = nn.Linear(D_e, D_h)
        self.smax_fc    = nn.Linear(9*D_h, 1)

    def _reverse_seq(self, X, mask):
        X_ = X.transpose(0,1)
        mask_sum = torch.sum(mask, 1).int()
        xfs = []
        for x, c in zip(X_, mask_sum):
            xf = torch.flip(x[:c], [0])
            xfs.append(xf)
        return pad_sequence(xfs)
    
    def forward(self, U_t, U_a, U_v, U_context, qmask, umask, return_embed = False):
        emotions_f_t,_ = self.multilogue_net_f_t(U_t, U_context, qmask)
        emotions_f_a,_ = self.multilogue_net_f_a(U_a, U_context, qmask)
        emotions_f_v,_ = self.multilogue_net_f_v(U_v, U_context, qmask)
        emotions_f_t = self.dropout_rec(emotions_f_t)
        emotions_f_a = self.dropout_rec(emotions_f_a)
        emotions_f_v = self.dropout_rec(emotions_f_v)
        hidden_t = (self.linear(emotions_f_t))
        hidden_a = (self.linear(emotions_f_a))
        hidden_v = (self.linear(emotions_f_v))
        hidden_t = (self.dropout(hidden_t))
        hidden_a = (self.dropout(hidden_a))
        hidden_v = (self.dropout(hidden_v))
        hidden_at = BiModalAttention(hidden_t, hidden_a) 
        hidden_tv = BiModalAttention(hidden_t, hidden_v)  
        hidden_va = BiModalAttention(hidden_v, hidden_a)  
        temp = self.smax_fc(torch.cat([hidden_at(),hidden_va(), hidden_tv(), hidden_t,hidden_a, hidden_v],dim=-1)).squeeze()
        if return_embed:
            return temp
        else:
            pred = 3 * torch.tanh(temp) 

            return pred.transpose(0,1).contiguous().view(-1)

class MaskedMSELoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.loss = nn.MSELoss(reduction='sum')

    def forward(self, pred, target, mask):
        loss = self.loss(pred*mask, target)/torch.sum(mask)
        return loss

class MaskedNLLLoss(nn.Module):
    def __init__(self, weight=None):
        super().__init__()
        self.weight = weight
        self.loss = nn.NLLLoss(weight=weight,
                               reduction='sum')

    def forward(self, pred, target, mask):
        mask_ = mask.view(-1,1) 
        if type(self.weight)==type(None):
            loss = self.loss(pred*mask_, target)/torch.sum(mask)
        else:
            loss = self.loss(pred*mask_, target)\
                            /torch.sum(self.weight[target]*mask_.squeeze())
        return loss

class UnMaskedWeightedNLLLoss(nn.Module):
    def __init__(self, weight=None):
        super().__init__()
        self.weight = weight
        self.loss = nn.NLLLoss(weight=weight,
                               reduction='sum')

    def forward(self, pred, target):
        if type(self.weight)==type(None):
            loss = self.loss(pred, target)
        else:
            loss = self.loss(pred, target)\
                            /torch.sum(self.weight[target])
        return loss