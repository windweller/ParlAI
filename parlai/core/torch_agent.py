#!/usr/bin/env python3

# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.
"""General utility code for building PyTorch-based agents in ParlAI.

Contains the following main utilities:
- TorchAgent class which serves as a useful parent class for other model agents
- Batch namedtuple which is the input type of the main abstract methods of
  the TorchAgent class
- Output namedtuple which is the expected output type of the main abstract
  methods of the TorchAgent class
- Beam class which provides some generic beam functionality for classes to use

See below for documentation on each specific tool.
"""

from parlai.core.agents import Agent
from parlai.core.build_data import modelzoo_path
from parlai.core.dict import DictionaryAgent
from parlai.core.utils import (set_namedtuple_defaults, argsort, padded_tensor,
                               NEAR_INF)

try:
    import torch
except ImportError as e:
    raise ImportError('Need to install Pytorch: go to pytorch.org')


from torch import optim
from collections import deque, namedtuple, Counter
import pickle
import random
import math
from operator import attrgetter

"""
Batch is a namedtuple containing data being sent to an agent.

This is the input type of the train_step and eval_step functions.
Agents can override the batchify function to return an extended namedtuple
with additional fields if they would like, though we recommend calling the
parent function to set up these fields as a base.

:field text_vec:       bsz x seqlen tensor containing the parsed text data.
:field text_lengths:   list of length bsz containing the lengths of the text in
                       same order as text_vec; necessary for
                       pack_padded_sequence.
:field label_vec:      bsz x seqlen tensor containing the parsed label (one per
                       batch row).
:field label_lengths:  list of length bsz containing the lengths of the labels
                       in same order as label_vec.
:field labels:         list of length bsz containing the selected label for
                       each batch row (some datasets have multiple labels per
                       input example).
:field valid_indices:  list of length bsz containing the original indices of
                       each example in the batch. we use these to map
                       predictions back to their proper row, since e.g. we may
                       sort examples by their length or some examples may be
                       invalid.
:field candidates:     list of lists of text. outer list has size bsz, inner
                       lists vary in size based on the number of candidates for
                       each row in the batch.
:field candidate_vecs: list of lists of tensors. outer list has size bsz, inner
                       lists vary in size based on the number of candidates for
                       each row in the batch.
:field image:          list of image features in the format specified by the
                       --image-mode arg.
"""
Batch = namedtuple('Batch', ['text_vec', 'text_lengths', 'label_vec',
                             'label_lengths', 'labels', 'valid_indices',
                             'candidates', 'candidate_vecs', 'image'])
set_namedtuple_defaults(Batch, default=None)


"""
Output is a namedtuple containing agent predictions.

This is the expected return type of the train_step and eval_step functions,
though agents can choose to return None if they do not want to answer.

:field text: list of strings of length bsz containing the predictions of the
             model
:field text_candidates: list of lists of length bsz containing ranked
                        predictions of the model. each sub-list is an ordered
                        ranking of strings, of variable length.
"""
Output = namedtuple('Output', ['text', 'text_candidates'])
set_namedtuple_defaults(Output, default=None)


class TorchAgent(Agent):
    """A provided base agent for any model that wants to use Torch.

    Exists to make it easier to implement a new agent.
    Not necessary, but reduces duplicated code.

    Many methods are intended to be either used as is when the default is
    acceptable, or to be overriden and called with super(), with the extra
    functionality added to the initial result. See the method comment for
    recommended behavior.

    This agent serves as a common framework for all ParlAI models which want
    to use PyTorch.
    """

    """Utility mapping of simple names to optimizers.

    For example:
    'adagrad': optim.Adagrad, 'adam': optim.Adad, 'sgd': optim.SGD
    """
    OPTIM_OPTS = {k.lower(): v for k, v in optim.__dict__.items()
                  if not k.startswith('__') and k[0].isupper()}

    P1_TOKEN = '__p1__'
    P2_TOKEN = '__p2__'

    @staticmethod
    def dictionary_class():
        """Return the dictionary class that this agent expects to use.

        Can be overriden if a more complex dictionary is required.
        """
        return DictionaryAgent

    @classmethod
    def add_cmdline_args(cls, argparser):
        """Add the default commandline args we expect most agents to want."""
        agent = argparser.add_argument_group('TorchAgent Arguments')
        # pretrained embedding arguments
        agent.add_argument(
            '-emb', '--embedding-type', default='random',
            choices=['random', 'glove', 'glove-fixed', 'glove-twitter-fixed',
                     'fasttext', 'fasttext-fixed', 'fasttext_cc',
                     'fasttext_cc-fixed'],
            help='Choose between different strategies for initializing word '
                 'embeddings. Default is random, but can also preinitialize '
                 'from Glove or Fasttext. Preinitialized embeddings can also '
                 'be fixed so they are not updated during training.')
        agent.add_argument(
            '-embp', '--embedding-projection', default='random',
            help='If pretrained embeddings have a different dimensionality '
                 'than your embedding size, strategy for projecting to the '
                 'correct size. If the dimensions are the same, this is '
                 'ignored unless you append "-force" to your choice.')
        # optimizer arguments
        agent.add_argument(
            '-opt', '--optimizer', default='sgd', choices=cls.OPTIM_OPTS,
            help='Choose between pytorch optimizers. Any member of torch.optim'
                 ' should be valid.')
        agent.add_argument(
            '-lr', '--learningrate', type=float, default=1,
            help='learning rate')
        agent.add_argument(
            '-clip', '--gradient-clip', type=float, default=0.1,
            help='gradient clipping using l2 norm')
        agent.add_argument(
            '-mom', '--momentum', default=0, type=float,
            help='if applicable, momentum value for optimizer.')
        # preprocessing arguments
        agent.add_argument(
            '-rc', '--rank-candidates', type='bool', default=False,
            help='Whether the model should parse candidates for ranking.')
        agent.add_argument(
            '-tr', '--truncate', default=-1, type=int,
            help='Truncate input lengths to increase speed / use less memory.')
        agent.add_argument(
            '-histsz', '--history-size', default=-1, type=int,
            help='Number of past dialog utterances to remember.')
        agent.add_argument(
            '-pt', '--person-tokens', type='bool', default=False,
            help='add person tokens to history. adds __p1__ in front of input '
                 'text and __p2__ in front of past labels when available or '
                 'past utterances generated by the model. these are added to '
                 'the dictionary during initialization.')
        # GPU arguments
        # these gpu options are all mutually exclusive, and should error if the
        # user tries to present multiple of them
        gpugroup = agent.add_mutually_exclusive_group()
        gpugroup.add_argument(
            '-gpu', '--gpu', type=int, default=-1, help='which GPU to use')
        gpugroup.add_argument(
            '--multigpu', type='bool', default=False,
            help='Suggest model should employ multiple GPUs. Not all models '
                 'respect this flag.')
        gpugroup.add_argument(
            '--no-cuda', type='bool', default=False,
            help='disable GPUs even if available. otherwise, will use GPUs if '
                 'available on the device.')

    def __init__(self, opt, shared=None):
        """Initialize agent."""
        super().__init__(opt, shared)
        opt = self.opt

        if not shared:
            # intitialize any important structures from scratch
            self.replies = {}  # past replies
            self.dict = self.dictionary_class()(opt)
            if opt.get('person_tokens'):
                self.dict[self.P1_TOKEN] = 999999999
                self.dict[self.P2_TOKEN] = 999999998
        else:
            # copy initialized data from shared table
            self.opt = shared['opt']
            self.dict = shared['dict']
            self.replies = shared['replies']

        if opt.get('numthreads', 1) > 1:
            torch.set_num_threads(1)

        # check for cuda
        self.use_cuda = not opt['no_cuda'] and torch.cuda.is_available()
        if self.use_cuda:
            if opt['multigpu'] and opt['gpu'] != -1:
                raise ValueError("Can't use --multigpu and --gpu together.")
            if not shared:
                print('[ Using CUDA ]')
            if not shared and opt['gpu'] != -1:
                torch.cuda.set_device(opt['gpu'])
            # It's up to the user of TorchAgent to handle multiple GPU issues.
            # A typical approach would be to employ nn.DataParallel
            # (https://pytorch.org/docs/0.4.1/nn.html?highlight=dataparallel#torch.nn.DataParallel)

        # now set up any fields that all instances may need
        self.id = 'TorchAgent'  # child can override
        self.EMPTY = torch.LongTensor([])
        self.NULL_IDX = self.dict[self.dict.null_token]
        self.START_IDX = self.dict[self.dict.start_token]
        self.END_IDX = self.dict[self.dict.end_token]

        self.random = random.Random(42)  # fixed random seed
        # which row in the batch this instance is
        self.batch_idx = shared and shared.get('batchindex') or 0
        # can remember as few as zero utterances if desired
        self.histsz = opt['history_size'] if opt['history_size'] >= 0 else None
        # stores up to hist_utt past observations within current dialog
        self.history = deque(maxlen=self.histsz)
        # truncate == 0 might give funny behavior
        self.truncate = opt['truncate'] if opt['truncate'] >= 0 else None
        self.rank_candidates = opt['rank_candidates']
        self.add_person_tokens = opt.get('person_tokens', False)

    def _init_optim(self, params, optim_states=None, saved_optim_type=None):
        """Initialize optimizer with model parameters.

        :param params:       parameters from the model, for example:
                             [p for p in model.parameters() if p.requires_grad]
        :param optim_states: optional argument providing states of optimizer
                             to load
        :saved_optim_type:   type of optimizer being loaded, if changed will
                             skip loading optimizer states
        """
        opt = self.opt
        # we only set up optimizers when training
        self.clip = opt.get('gradient_clip', -1)

        # set up optimizer args
        # TODO: move optimizer params to command line args
        lr = opt['learningrate']
        kwargs = {'lr': lr}
        if opt.get('momentum') > 0 and opt['optimizer'] in ['sgd', 'rmsprop']:
            kwargs['momentum'] = opt['momentum']
            if opt['optimizer'] == 'sgd':
                kwargs['nesterov'] = True
        if opt['optimizer'] == 'adam':
            # amsgrad paper: https://openreview.net/forum?id=ryQu7f-RZ
            kwargs['amsgrad'] = True

        optim_class = self.OPTIM_OPTS[opt['optimizer']]
        self.optimizer = optim_class(params, **kwargs)
        if optim_states:
            if saved_optim_type != opt['optimizer']:
                print('WARNING: not loading optim state since optim class '
                      'changed.')
            else:
                try:
                    self.optimizer.load_state_dict(optim_states)
                except ValueError:
                    print('WARNING: not loading optim state since model '
                          'params changed.')
                if self.use_cuda:
                    for state in self.optimizer.state.values():
                        for k, v in state.items():
                            if isinstance(v, torch.Tensor):
                                state[k] = v.cuda()
        # TODO: Move scheduler params to command line args
        self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            self.optimizer, 'min', factor=0.5, patience=3, verbose=True)

    def receive_metrics(self, metrics_dict):
        """Use the metrics to decide when to adjust LR schedule.

        This uses the loss as the validation metric if present, if not this
        function does nothing. Note that the model must be reporting loss for
        this to work.
        Override this to override the behavior.
        """
        if 'loss' in metrics_dict:
            self.scheduler.step(metrics_dict['loss'])

    def _get_embtype(self, emb_type):
        # set up preinitialized embeddings
        try:
            import torchtext.vocab as vocab
        except ImportError as ex:
            print('Please install torch text with `pip install torchtext`')
            raise ex
        pretrained_dim = 300
        if emb_type.startswith('glove'):
            if 'twitter' in emb_type:
                init = 'glove-twitter'
                name = 'twitter.27B'
                pretrained_dim = 200
            else:
                init = 'glove'
                name = '840B'
            embs = vocab.GloVe(
                name=name, dim=pretrained_dim,
                cache=modelzoo_path(self.opt.get('datapath'),
                                    'models:glove_vectors'))
        elif emb_type.startswith('fasttext_cc'):
            init = 'fasttext_cc'
            embs = vocab.FastText(
                language='en',
                cache=modelzoo_path(self.opt.get('datapath'),
                                    'models:fasttext_cc_vectors'))
        elif emb_type.startswith('fasttext'):
            init = 'fasttext'
            embs = vocab.FastText(
                language='en',
                cache=modelzoo_path(self.opt.get('datapath'),
                                    'models:fasttext_vectors'))
        else:
            raise RuntimeError('embedding type {} not implemented. check arg, '
                               'submit PR to this function, or override it.'
                               ''.format(emb_type))
        return embs, init

    def _project_vec(self, vec, target_dim, method='random'):
        """If needed, project vector to target dimensionality.

        Projection methods implemented are the following:

        random - random gaussian matrix multiplication of input vector

        :param vec:        one-dimensional vector
        :param target_dim: dimension of returned vector
        :param method:     projection method. will be used even if the dim is
                           not changing if method ends in "-force".
        """
        pre_dim = vec.size(0)
        if pre_dim != target_dim or method.endswith('force'):
            if method.startswith('random'):
                # random projection
                if not hasattr(self, 'proj_rp'):
                    self.proj_rp = torch.Tensor(pre_dim, target_dim).normal_()
                    # rescale so we're not destroying norms too much
                    # http://scikit-learn.org/stable/modules/random_projection.html#gaussian-random-projection
                    self.proj_rp /= target_dim
                return torch.mm(vec.unsqueeze(0), self.proj_rp)
            else:
                # TODO: PCA
                # TODO: PCA + RP
                # TODO: copy
                raise RuntimeError('Projection method not implemented: {}'
                                   ''.format(method))
        else:
            return vec

    def _copy_embeddings(self, weight, emb_type, log=True):
        """Copy embeddings from the pretrained embeddings to the lookuptable.

        :param weight:   weights of lookup table (nn.Embedding/nn.EmbeddingBag)
        :param emb_type: pretrained embedding type
        """
        embs, name = self._get_embtype(emb_type)
        cnt = 0
        for w, i in self.dict.tok2ind.items():
            if w in embs.stoi:
                vec = self._project_vec(embs.vectors[embs.stoi[w]],
                                        weight.size(1))
                weight.data[i] = vec
                cnt += 1

        if log:
            print('Initialized embeddings for {} tokens ({}%) from {}.'
                  ''.format(cnt, round(cnt * 100 / len(self.dict), 1), name))

    def share(self):
        """Share fields from parent as well as useful objects in this class.

        Subclasses will likely want to share their model as well.
        """
        shared = super().share()
        shared['opt'] = self.opt
        shared['dict'] = self.dict
        shared['replies'] = self.replies
        return shared

    def _v2t(self, vec):
        """Convert token indices to string of tokens."""
        new_vec = []
        if hasattr(vec, 'cpu'):
            vec = vec.cpu()
        for i in vec:
            if i == self.END_IDX:
                break
            new_vec.append(i)
        return self.dict.vec2txt(new_vec)

    def _vectorize_text(self, text, add_start=False, add_end=False,
                        truncate=None, truncate_left=True):
        """Return vector from text.

        :param text:          String to vectorize.
        :param add_start:     Add the start token to the front of the tensor.
        :param add_end:       Add the end token to the end of the tensor.
        :param truncate:      Truncate to this many tokens >= 0, or None.
        :param truncate_left: Truncate from the left side (keep the rightmost
                              tokens). You probably want this True for inputs,
                              False for targets.
        """
        vec = self.dict.txt2vec(text)
        if truncate is None or len(vec) + add_start + add_end < truncate:
            # simple: no truncation
            if add_start:
                vec.insert(0, self.START_IDX)
            if add_end:
                vec.append(self.END_IDX)
        elif truncate_left:
            # don't check add_start, we know are truncating it
            if add_end:
                # add the end token first
                vec.append(self.END_IDX)
            vec = vec[len(vec) - truncate:]
        else:
            # truncate from the right side
            # don't check add_end, we know we are truncating it
            vec = vec[:truncate - add_start]
            if add_start:
                # always keep the start token if it's there
                vec.insert(0, self.START_IDX)
        tensor = torch.LongTensor(vec)
        return tensor

    def _check_truncate(self, vec, truncate):
        """Check that vector is truncated correctly."""
        if truncate is None:
            return vec
        if len(vec) <= truncate:
            return vec
        else:
            return vec[:truncate]

    def _set_text_vec(self, obs, add_start, add_end, truncate):
        """Sets the 'text_vec' field in the observation."""

        if 'text_vec' in obs:
            # check truncation of pre-computed vectors
            obs['text_vec'] = self._check_truncate(obs['text_vec'], truncate)
        elif 'text' in obs:
            # convert 'text' into tensor of dictionary indices
            # we don't add start and end to the input
            obs['text_vec'] = self._vectorize_text(obs['text'], truncate=truncate)
        return obs

    def _set_label_vec(self, obs, add_start, add_end, truncate):
        """Sets the 'labels_vec' field in the observation."""

        # convert 'labels' or 'eval_labels' into vectors
        if 'labels' in obs:
            label_type = 'labels'
        elif 'eval_labels' in obs:
            label_type = 'eval_labels'
        else:
            label_type = None

        if label_type is None:
            return

        elif label_type + '_vec' in obs:
            # check truncation of pre-computed vector
            obs[label_type + '_vec'] = self._check_truncate(
                obs[label_type + '_vec'], truncate)
        else:
            # pick one label if there are multiple
            lbls = obs[label_type]
            label = lbls[0] if len(lbls) == 1 else self.random.choice(lbls)
            vec_label = self._vectorize_text(label, add_start, add_end,
                                             truncate, False)
            obs[label_type + '_vec'] = vec_label
            obs[label_type + '_choice'] = label

        return obs

    def _set_label_cands_vec(self, obs, add_start, add_end, truncate):
        """Sets the 'label_candidates_vec' field in the observation."""

        if 'label_candidates_vecs' in obs:
            if truncate is not None:
                # check truncation of pre-computed vectors
                vecs = obs['label_candidates_vecs']
                for i, c in enumerate(vecs):
                    vecs[i] = self._check_truncate(c, truncate)
        elif self.rank_candidates and 'label_candidates' in obs:
            obs['label_candidates'] = list(obs['label_candidates'])
            obs['label_candidates_vecs'] = [
                self._vectorize_text(c, add_start, add_end, truncate, False)
                for c in obs['label_candidates']]
        return obs

    def vectorize(self, obs, add_start=True, add_end=True, truncate=None):
        """Make vectors out of observation fields and store in the observation.

        In particular, the 'text' and 'labels'/'eval_labels' fields are
        processed and a new field is added to the observation with the suffix
        '_vec'.

        If you want to use additional fields on your subclass, you can override
        this function, call super().vectorize(...) to process the text and
        labels, and then process the other fields in your subclass.

        :param obs:         Single observation from observe function.
        :param add_start:   default True, adds the start token to each label.
        :param add_end:     default True, adds the end token to each label.
        :param truncate:    default None, if set truncates all vectors to the
                            specified length. Note that this truncates to the
                            rightmost for inputs and the leftmost for labels
                            and, when applicable, candidates.
        """
        self._set_text_vec(obs, add_start, add_end, truncate)
        self._set_label_vec(obs, add_start, add_end, truncate)
        self._set_label_cands_vec(obs, add_start, add_end, truncate)
        return obs

    def batchify(self, obs_batch, sort=False,
                 is_valid=lambda obs: 'text_vec' in obs or 'image' in obs):
        """Create a batch of valid observations from an unchecked batch.

        A valid observation is one that passes the lambda provided to the
        function, which defaults to checking if the preprocessed 'text_vec'
        field is present which would have been set by this agent's 'vectorize'
        function.

        Returns a namedtuple Batch. See original definition above for in-depth
        explanation of each field.

        If you want to include additonal fields in the batch, you can subclass
        this function and return your own "Batch" namedtuple: copy the Batch
        namedtuple at the top of this class, and then add whatever additional
        fields that you want to be able to access. You can then call
        super().batchify(...) to set up the original fields and then set up the
        additional fields in your subclass and return that batch instead.

        :param obs_batch: List of vectorized observations
        :param sort:      Default False, orders the observations by length of
                          vectors. Set to true when using
                          torch.nn.utils.rnn.pack_padded_sequence.
                          Uses the text vectors if available, otherwise uses
                          the label vectors if available.
        :param is_valid:  Function that checks if 'text_vec' is in the
                          observation, determines if an observation is valid
        """
        if len(obs_batch) == 0:
            return Batch()

        valid_obs = [(i, ex) for i, ex in enumerate(obs_batch) if is_valid(ex)]

        if len(valid_obs) == 0:
            return Batch()

        valid_inds, exs = zip(*valid_obs)

        # TEXT
        xs, x_lens = None, None
        if any('text_vec' in ex for ex in exs):
            _xs = [ex.get('text_vec', self.EMPTY) for ex in exs]
            xs, x_lens = padded_tensor(_xs, self.NULL_IDX, self.use_cuda)
            if sort:
                sort = False  # now we won't sort on labels
                xs, x_lens, valid_inds, exs = argsort(
                    x_lens, xs, x_lens, valid_inds, exs, descending=True
                )

        # LABELS
        labels_avail = any('labels_vec' in ex for ex in exs)
        some_labels_avail = (labels_avail or
                             any('eval_labels_vec' in ex for ex in exs))

        ys, y_lens, labels = None, None, None
        if some_labels_avail:
            field = 'labels' if labels_avail else 'eval_labels'

            label_vecs = [ex.get(field + '_vec', self.EMPTY) for ex in exs]
            labels = [ex.get(field + '_choice') for ex in exs]
            y_lens = [y.shape[0] for y in label_vecs]

            ys, y_lens = padded_tensor(label_vecs, self.NULL_IDX, self.use_cuda)
            if sort and xs is None:
                ys, valid_inds, label_vecs, labels, y_lens = argsort(
                    y_lens, ys, valid_inds, label_vecs, labels, y_lens,
                    descending=True
                )

        # LABEL_CANDIDATES
        cands, cand_vecs = None, None
        if any('label_candidates_vecs' in ex for ex in exs):
            cands = [ex.get('label_candidates', None) for ex in exs]
            cand_vecs = [ex.get('label_candidates_vecs', None) for ex in exs]

        # IMAGE
        imgs = None
        if any('image' in ex for ex in exs):
            imgs = [ex.get('image', None) for ex in exs]

        return Batch(text_vec=xs, text_lengths=x_lens, label_vec=ys,
                     label_lengths=y_lens, labels=labels,
                     valid_indices=valid_inds, candidates=cands,
                     candidate_vecs=cand_vecs, image=imgs)

    def match_batch(self, batch_reply, valid_inds, output=None):
        """Match sub-batch of predictions to the original batch indices.

        Batches may be only partially filled (i.e when completing the remainder
        at the end of the validation or test set), or we may want to sort by
        e.g the length of the input sequences if using pack_padded_sequence.

        This matches rows back with their original row in the batch for
        calculating metrics like accuracy.

        If output is None (model choosing not to provide any predictions), we
        will just return the batch of replies.

        Otherwise, output should be a parlai.core.torch_agent.Output object.
        This is a namedtuple, which can provide text predictions and/or
        text_candidates predictions. If you would like to map additional
        fields into the batch_reply, you can override this method as well as
        providing your own namedtuple with additional fields.

        :param batch_reply: Full-batchsize list of message dictionaries to put
            responses into.
        :param valid_inds: Original indices of the predictions.
        :param output: Output namedtuple which contains sub-batchsize list of
            text outputs from model. May be None (default) if model chooses not
            to answer. This method will check for ``text`` and
            ``text_candidates`` fields.
        """
        if output is None:
            return batch_reply
        if output.text is not None:
            for i, response in zip(valid_inds, output.text):
                batch_reply[i]['text'] = response
        if output.text_candidates is not None:
            for i, cands in zip(valid_inds, output.text_candidates):
                batch_reply[i]['text_candidates'] = cands
        return batch_reply

    def _add_person_tokens(self, text, token, add_after_newln=False):
        if add_after_newln:
            split = text.split('\n')
            split[-1] = token + ' ' + split[-1]
            return '\n'.join(split)
        else:
            return token + ' ' + text

    def get_dialog_history(self, observation, reply=None,
                           add_person_tokens=False, add_p1_after_newln=False):
        """Retrieve dialog history and add current observations to it.

        :param observation:        current observation
        :param reply:              past utterance from the model to add to the
                                   history, such as the past label or response
                                   generated by the model.
        :param add_person_tokens:  add tokens identifying each speaking before
                                   utterances in the text & history.
        :param add_p1_after_newln: add the other speaker token before the last
                                   newline in the input instead of at the
                                   beginning of the input. this is useful for
                                   tasks that include some kind of context
                                   before the actual utterance (e.g. squad,
                                   babi, personachat).

        :return: observation with text replaced with full dialog
        """
        obs = observation

        if reply is not None:
            if add_person_tokens:
                # add person2 token to reply
                reply = self._add_person_tokens(reply, self.P2_TOKEN)
            # add reply to history
            self.history.append(reply)

        if 'text' in obs:
            if add_person_tokens:
                # add person1 token to text
                obs['text'] = self._add_person_tokens(obs['text'],
                                                      self.P1_TOKEN,
                                                      add_p1_after_newln)
            # add text to history
            self.history.append(obs['text'])

        if len(self.history) > 0:
            obs['text'] = '\n'.join(self.history)
        if obs.get('episode_done', True):
            # end of this episode, clear the history
            self.history.clear()
        return obs

    def last_reply(self, use_label=True):
        """Retrieve the last reply from the model.

        If available, we use the true label instead of the model's prediction.

        By default, batch_act stores the batch of replies and this method
        will extract the reply of the current instance from the batch.

        :param use_label: default true, use the label when available instead of
                          the model's generated response.
        """
        # if the last observation was the end of an episode,
        # then we shouldn't use it as history
        if not self.observation or self.observation.get('episode_done', True):
            return None

        if use_label:
            # first look for the true label, if we aren't on a new episode
            label_key = ('labels' if 'labels' in self.observation else
                         'eval_labels' if 'eval_labels' in self.observation
                         else None)
            if label_key is not None:
                lbls = self.observation[label_key]
                last_reply = (lbls[0] if len(lbls) == 1
                              else self.random.choice(lbls))
                return last_reply
        # otherwise, we use the last reply the model generated
        batch_reply = self.replies.get('batch_reply')
        if batch_reply is not None:
            return batch_reply[self.batch_idx].get('text')
        return None

    def observe(self, observation):
        """Process incoming message in preparation for producing a response.

        This includes remembering the past history of the conversation.
        """
        reply = self.last_reply()
        self.observation = self.get_dialog_history(
            observation, reply=reply, add_person_tokens=self.add_person_tokens)
        return self.vectorize(self.observation, truncate=self.truncate)

    def save(self, path=None):
        """Save model parameters to path (or default to model_file arg).

        Override this method for more specific saving.
        """
        path = self.opt.get('model_file', None) if path is None else path

        if path:
            states = {}
            if hasattr(self, 'model'):  # save model params
                states['model'] = self.model.state_dict()
            if hasattr(self, 'optimizer'):  # save optimizer params
                states['optimizer'] = self.optimizer.state_dict()

            if states:  # anything found to save?
                with open(path, 'wb') as write:
                    torch.save(states, write)

                # save opt file
                with open(path + ".opt", 'wb') as handle:
                    if hasattr(self, 'model_version'):
                        self.opt['model_version'] = self.model_version()
                    pickle.dump(self.opt, handle,
                                protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, path):
        """Return opt and model states.

        Override this method for more specific loading.
        """
        states = torch.load(path, map_location=lambda cpu, _: cpu)
        if 'model' in states:
            self.model.load_state_dict(states['model'])
        if 'optimizer' in states and hasattr(self, 'optimizer'):
            self.optimizer.load_state_dict(states['optimizer'])
        return states

    def reset(self):
        """Clear internal states."""
        self.observation = None
        self.history.clear()
        self.replies.clear()
        self.reset_metrics()

    def act(self):
        """Call batch_act with the singleton batch."""
        return self.batch_act([self.observation])[0]

    def batch_act(self, observations):
        """Process a batch of observations (batchsize list of message dicts).

        These observations have been preprocessed by the observe method.

        Subclasses can override this for special functionality, but if the
        default behaviors are fine then just override the ``train_step`` and
        ``eval_step`` methods instead. The former is called when labels are
        present in the observations batch; otherwise, the latter is called.
        """
        batch_size = len(observations)
        # initialize a list of replies with this agent's id
        batch_reply = [{'id': self.getID()} for _ in range(batch_size)]

        # check if there are any labels available, if so we will train on them
        is_training = any(['labels' in obs for obs in observations])

        # create a batch from the vectors
        batch = self.batchify(observations)

        if is_training:
            output = self.train_step(batch)
        else:
            output = self.eval_step(batch)

        if output is None:
            self.replies['batch_reply'] = None
            return batch_reply

        self.match_batch(batch_reply, batch.valid_indices, output)
        self.replies['batch_reply'] = batch_reply

        return batch_reply

    def train_step(self, batch):
        """Process one batch with training labels."""
        raise NotImplementedError(
            'Abstract class: user must implement train_step')

    def eval_step(self, batch):
        """Process one batch but do not train on it."""
        raise NotImplementedError(
            'Abstract class: user must implement eval_step')


class Beam(object):
    """Generic beam class. It keeps information about beam_size hypothesis."""

    def __init__(self, beam_size, min_length=3, padding_token=0, bos_token=1,
                 eos_token=2, min_n_best=3, cuda='cpu', block_ngram=0):
        """Instantiate Beam object.

        :param beam_size: number of hypothesis in the beam
        :param min_length: minimum length of the predicted sequence
        :param padding_token: Set to 0 as usual in ParlAI
        :param bos_token: Set to 1 as usual in ParlAI
        :param eos_token: Set to 2 as usual in ParlAI
        :param min_n_best: Beam will not be done unless this amount of finished
                           hypothesis (with EOS) is done
        :param cuda: What device to use for computations
        """
        self.beam_size = beam_size
        self.min_length = min_length
        self.eos = eos_token
        self.bos = bos_token
        self.pad = padding_token
        self.device = cuda
        # recent score for each hypo in the beam
        self.scores = torch.Tensor(self.beam_size).float().zero_().to(
            self.device)
        # self.scores values per each time step
        self.all_scores = [torch.Tensor([0.0] * beam_size).to(self.device)]
        # backtracking id to hypothesis at previous time step
        self.bookkeep = []
        # output tokens at each time step
        self.outputs = [torch.Tensor(self.beam_size).long()
                        .fill_(self.bos).to(self.device)]
        # keeps tuples (score, time_step, hyp_id)
        self.finished = []
        self.HypothesisTail = namedtuple(
            'HypothesisTail', ['timestep', 'hypid', 'score', 'tokenid'])
        self.eos_top = False
        self.eos_top_ts = None
        self.n_best_counter = 0
        self.min_n_best = min_n_best
        self.block_ngram = block_ngram

    @staticmethod
    def find_ngrams(input_list, n):
        """Get list of ngrams with context length n-1"""
        return list(zip(*[input_list[i:] for i in range(n)]))

    def get_output_from_current_step(self):
        """Get the outputput at the current step."""
        return self.outputs[-1]

    def get_backtrack_from_current_step(self):
        """Get the backtrack at the current step."""
        return self.bookkeep[-1]

    def advance(self, softmax_probs):
        """Advance the beam one step."""
        voc_size = softmax_probs.size(-1)
        current_length = len(self.all_scores) - 1
        if current_length < self.min_length:
            # penalize all eos probs to make it decode longer
            for hyp_id in range(softmax_probs.size(0)):
                softmax_probs[hyp_id][self.eos] = -NEAR_INF
        if len(self.bookkeep) == 0:
            # the first step we take only the first hypo into account since all
            # hypos are the same initially
            beam_scores = softmax_probs[0]
        else:
            # we need to sum up hypo scores and curr softmax scores before topk
            # [beam_size, voc_size]
            beam_scores = (softmax_probs +
                           self.scores.unsqueeze(1).expand_as(softmax_probs))
            for i in range(self.outputs[-1].size(0)):
                current_hypo = [ii.tokenid.item() for ii in
                                self.get_partial_hyp_from_tail(
                                len(self.outputs) - 1, i)][::-1][1:]
                if self.block_ngram > 0:
                    current_ngrams = []
                    for ng in range(self.block_ngram):
                        ngrams = Beam.find_ngrams(current_hypo, ng)
                        if len(ngrams) > 0:
                            current_ngrams.extend(ngrams)
                    counted_ngrams = Counter(current_ngrams)
                    if any(v > 1 for k, v in counted_ngrams.items()):
                        # block this hypothesis hard
                        beam_scores[i] = -NEAR_INF

                #  if previous output hypo token had eos
                # we penalize those word probs to never be chosen
                if self.outputs[-1][i] == self.eos:
                    # beam_scores[i] is voc_size array for i-th hypo
                    beam_scores[i] = -NEAR_INF

        flatten_beam_scores = beam_scores.view(-1)  # [beam_size * voc_size]
        with torch.no_grad():
            best_scores, best_idxs = torch.topk(
                flatten_beam_scores, self.beam_size, dim=-1)

        self.scores = best_scores
        self.all_scores.append(self.scores)
        # get the backtracking hypothesis id as a multiple of full voc_sizes
        hyp_ids = best_idxs / voc_size
        # get the actual word id from residual of the same division
        tok_ids = best_idxs % voc_size

        self.outputs.append(tok_ids)
        self.bookkeep.append(hyp_ids)

        #  check new hypos for eos label, if we have some, add to finished
        for hypid in range(self.beam_size):
            if self.outputs[-1][hypid] == self.eos:
                #  this is finished hypo, adding to finished
                eostail = self.HypothesisTail(timestep=len(self.outputs) - 1,
                                              hypid=hypid,
                                              score=self.scores[hypid],
                                              tokenid=self.eos)
                self.finished.append(eostail)
                self.n_best_counter += 1

        if self.outputs[-1][0] == self.eos:
            self.eos_top = True
            if self.eos_top_ts is None:
                self.eos_top_ts = len(self.outputs) - 1

    def done(self):
        """Return whether beam search is complete."""
        return self.eos_top and self.n_best_counter >= self.min_n_best

    def get_top_hyp(self):
        """Get single best hypothesis.

        :return: hypothesis sequence and the final score
        """
        top_hypothesis_tail = self.get_rescored_finished(n_best=1)[0]
        return (self.get_hyp_from_finished(top_hypothesis_tail),
                top_hypothesis_tail.score)

    def get_hyp_from_finished(self, hypothesis_tail):
        """Extract hypothesis ending with EOS at timestep with hyp_id.

        :param timestep: timestep with range up to len(self.outputs)-1
        :param hyp_id: id with range up to beam_size-1
        :return: hypothesis sequence
        """
        assert (self.outputs[hypothesis_tail.timestep]
                [hypothesis_tail.hypid] == self.eos)
        assert hypothesis_tail.tokenid == self.eos
        hyp_idx = []
        endback = hypothesis_tail.hypid
        for i in range(hypothesis_tail.timestep, -1, -1):
            hyp_idx.append(self.HypothesisTail(
                timestep=i, hypid=endback, score=self.all_scores[i][endback],
                tokenid=self.outputs[i][endback]))
            endback = self.bookkeep[i - 1][endback]

        return hyp_idx

    @staticmethod
    def get_pretty_hypothesis(list_of_hypotails):
        """Return prettier version of the hypotheses."""
        hypothesis = []
        for i in list_of_hypotails:
            hypothesis.append(i.tokenid)

        hypothesis = torch.stack(list(reversed(hypothesis)))

        return hypothesis

    def get_partial_hyp_from_tail(self, ts, hypid):
        hypothesis_tail = self.HypothesisTail(
            timestep=ts,
            hypid=torch.Tensor([hypid]).long(),
            score=self.all_scores[ts][hypid],
            tokenid=self.outputs[ts][hypid])
        hyp_idx = []
        endback = hypothesis_tail.hypid
        for i in range(hypothesis_tail.timestep, -1, -1):
            hyp_idx.append(self.HypothesisTail(
                timestep=i,
                hypid=endback,
                score=self.all_scores[i][endback],
                tokenid=self.outputs[i][endback]))
            endback = self.bookkeep[i - 1][endback]

        return hyp_idx

    def get_rescored_finished(self, n_best=None):
        """Return finished hypotheses in rescored order.

        :param n_best: how many n best hypothesis to return
        :return: list with hypothesis
        """
        rescored_finished = []
        for finished_item in self.finished:
            current_length = finished_item.timestep + 1
            # these weights are from Google NMT paper
            length_penalty = math.pow((1 + current_length) / 6, 0.65)
            rescored_finished.append(self.HypothesisTail(
                timestep=finished_item.timestep, hypid=finished_item.hypid,
                score=finished_item.score / length_penalty,
                tokenid=finished_item.tokenid))

        srted = sorted(rescored_finished, key=attrgetter('score'),
                       reverse=True)

        if n_best is not None:
            srted = srted[:n_best]

        return srted

    def check_finished(self):
        """Check if self.finished is empty and add hyptail in that case.

        This will be suboptimal hypothesis since the model did not get any EOS

        :returns: None
        """
        if len(self.finished) == 0:
            # we change output because we want outputs to have eos
            # to pass assert in L102, it is ok since empty self.finished
            # means junk prediction anyway
            self.outputs[-1][0] = self.eos
            hyptail = self.HypothesisTail(timestep=len(self.outputs) - 1,
                                          hypid=0,
                                          score=self.all_scores[-1][0],
                                          tokenid=self.outputs[-1][0])

            self.finished.append(hyptail)

    def get_beam_dot(self, dictionary=None, n_best=None):
        """Create pydot graph representation of the beam.

        :param outputs: self.outputs from the beam
        :param dictionary: tok 2 word dict to save words in the tree nodes
        :returns: pydot graph
        """
        try:
            import pydot
        except ImportError:
            print("Please install pydot package to dump beam visualization")

        graph = pydot.Dot(graph_type='digraph')
        outputs = [i.tolist() for i in self.outputs]
        bookkeep = [i.tolist() for i in self.bookkeep]
        all_scores = [i.tolist() for i in self.all_scores]
        if n_best is None:
            n_best = int(self.beam_size / 2)

        # get top nbest hyp
        top_hyp_idx_n_best = []
        n_best_colors = ['aquamarine', 'chocolate1', 'deepskyblue',
                         'green2', 'tan']
        sorted_finished = self.get_rescored_finished(n_best=n_best)
        for hyptail in sorted_finished:
            # do not include EOS since it has rescored score not from original
            # self.all_scores, we color EOS with black
            top_hyp_idx_n_best.append(self.get_hyp_from_finished(
                hyptail))

        # create nodes
        for tstep, lis in enumerate(outputs):
            for hypid, token in enumerate(lis):
                if tstep == 0:
                    hypid = 0  # collapse all __NULL__ nodes
                node_tail = self.HypothesisTail(timestep=tstep, hypid=hypid,
                                                score=all_scores[tstep][hypid],
                                                tokenid=token)
                color = 'white'
                rank = None
                for i, hypseq in enumerate(top_hyp_idx_n_best):
                    if node_tail in hypseq:
                        if n_best <= 5:  # color nodes only if <=5
                            color = n_best_colors[i]
                        rank = i
                        break
                label = (
                    "<{}".format(dictionary.vec2txt([token])
                                 if dictionary is not None else token) +
                    " : " +
                    "{:.{prec}f}>".format(all_scores[tstep][hypid], prec=3))

                graph.add_node(pydot.Node(
                    node_tail.__repr__(), label=label, fillcolor=color,
                    style='filled',
                    xlabel='{}'.format(rank) if rank is not None else ''))

        # create edges
        for revtstep, lis in reversed(list(enumerate(bookkeep))):
            for i, prev_id in enumerate(lis):
                from_node = graph.get_node(
                    '"{}"'.format(self.HypothesisTail(
                        timestep=revtstep, hypid=prev_id,
                        score=all_scores[revtstep][prev_id],
                        tokenid=outputs[revtstep][prev_id]).__repr__()))[0]
                to_node = graph.get_node(
                    '"{}"'.format(self.HypothesisTail(
                        timestep=revtstep + 1, hypid=i,
                        score=all_scores[revtstep + 1][i],
                        tokenid=outputs[revtstep + 1][i]).__repr__()))[0]
                newedge = pydot.Edge(from_node.get_name(), to_node.get_name())
                graph.add_edge(newedge)

        return graph
