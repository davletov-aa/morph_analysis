import argparse
import json
import pickle
from datetime import datetime

import numpy as np
import tensorflow as tf
from tensorflow.python.ops.rnn import dynamic_rnn
from sklearn.utils import shuffle
from tensorflow.keras.layers import Bidirectional, LSTM
from tqdm import tqdm_notebook, tqdm

from_notebook = True


class BiLSTMClassifier:
    def __init__(self, config_path='build_config.json', is_training=True, pad_idx=0, chkp_dir='.', merge_mode='ave',
                 weight_decay=0.0):
        self.config_path = config_path
        self.is_training = is_training
        self.pad_idx = pad_idx
        self.chkp_dir = chkp_dir
        self.merge_mode = merge_mode
        self.weight_decay = weight_decay
        tf.reset_default_graph()
        self.global_step = tf.train.get_or_create_global_step()
        self.build_model()
        self.extra_vars_to_save = [(self.global_step.op.name, self.global_step), (self.lr.op.name, self.lr)]
        self.variables_to_save = dict(
            self.extra_vars_to_save + [(x.op.name, x) for x in
                                       tf.trainable_variables()])
        self.saver = tf.train.Saver(self.variables_to_save)

    @staticmethod
    def create_config(save_path, config):
        with open(save_path, 'w') as f:
            json.dump(config, f)
        return config

    @staticmethod
    def load_config(save_path):
        with open(save_path, 'r') as f:
            res = json.load(f)
        return res

    def build_model(self):
        config = self.load_config(self.config_path)
        max_seq_len = config['max_seq_len']
        hidden_size = config['hidden_size']
        voc_size = config['voc_size']
        num_layers = config['num_layers']
        num_classes = config['num_classes']
        lr = config['learning_rate']

        self.dropout = tf.placeholder(tf.float32, shape=[], name='dropout')
        self.keep_prob = 1.0 - self.dropout
        self.inputs = tf.placeholder(tf.int32, shape=[None, None], name='inputs')
        self.targets = tf.placeholder(tf.int32, shape=[None, None], name='targets')
        self.weights = tf.placeholder(tf.float32, shape=[None, None], name='weights')

        self.lr = tf.get_variable(initializer=lr, trainable=False, name='learning_rate')
        tf.summary.scalar('lr__', self.lr)
        with tf.device("/cpu:0"):
            embeddings = tf.get_variable(
                "embeddings", [voc_size, hidden_size], dtype=tf.float32)
            inputs = tf.nn.embedding_lookup(embeddings, self.inputs)
            if self.is_training:
                inputs = tf.nn.dropout(inputs, keep_prob=self.keep_prob)

        intermediate_size = 2 * hidden_size if self.merge_mode == 'concat' else hidden_size
        model = None

        # lstm_input = tf.python.ops.rnn.dynamic_rnn()
        # dynamic_rnn()
        for i in range(num_layers):
            if i == 0:
                model = Bidirectional(LSTM(hidden_size, return_sequences=True), merge_mode=self.merge_mode,
                                      input_shape=(max_seq_len, hidden_size))(inputs)
            else:
                if self.is_training:
                    model = tf.nn.dropout(model, keep_prob=self.keep_prob)
                model = Bidirectional(LSTM(hidden_size, return_sequences=True), merge_mode=self.merge_mode,
                                      input_shape=(max_seq_len, intermediate_size))(model)
        self.dense = tf.get_variable('dense', [intermediate_size, num_classes], dtype=tf.float32)

        model = tf.reshape(model, [-1, intermediate_size])
        self.model = tf.reshape(tf.matmul(model, self.dense), [-1, max_seq_len, num_classes])
        self.predictions = tf.argmax(self.model, axis=-1)

        if not self.is_training:
            return

        self.loss = tf.contrib.seq2seq.sequence_loss(logits=self.model,
                                                     targets=self.targets,
                                                     weights=self.weights,
                                                     average_across_timesteps=True,
                                                     average_across_batch=False,
                                                     name='loss')
        self.loss_averaged = tf.reduce_mean(self.loss)
        tf.summary.scalar('loss__', self.loss_averaged)

        l2_loss = tf.constant(0.0, dtype=tf.float32)
        for var in tf.trainable_variables():
            l2_loss += tf.nn.l2_loss(var)

        self.l2_loss = self.loss_averaged + l2_loss * self.weight_decay
        tf.summary.scalar('loss_l2__', self.l2_loss)

        print('targets shape:', self.targets.get_shape())
        print('predictions shape:', self.predictions.get_shape())
        print('weights shape:', self.weights.get_shape())
        correct = tf.cast(tf.equal(self.targets, tf.cast(self.predictions, tf.int32)), tf.float32)
        correct = tf.reduce_sum(tf.multiply(correct, self.weights), name='correct_predictions')
        total = tf.reduce_sum(self.weights, name='total_predictions')
        tf.summary.scalar('correct_predictions__', correct)
        tf.summary.scalar('total_predictions__', total)

        self.accuracy = tf.math.divide(correct, total, name='accuracy')
        tf.summary.scalar('accuracy__', self.accuracy)

        self.summaries = tf.summary.merge_all()

        if config['optimizer'].lower() == 'adam':
            self.optimizer = tf.train.AdamOptimizer(self.lr)
        elif config['optimizer'].lower() == 'sgd':
            self.optimizer = tf.train.GradientDescentOptimizer(self.lr)
        elif config['optimizer'].lower() == 'rmsprop':
            self.optimizer = tf.train.RMSPropOptimizer(self.lr)
        elif config['optimizer'].lower() == 'adagrad':
            self.optimizer = tf.train.AdagradOptimizer(self.lr)
        else:
            raise ValueError()

        trainable_variables = tf.trainable_variables()
        grads = tf.gradients(self.l2_loss, trainable_variables)
        self.train = self.optimizer.apply_gradients(zip(grads, trainable_variables), global_step=self.global_step,
                                                    name='train')

    def fit(self, inputs, targets, batch_size=30, epochs=5, from_chkp=None, dropout=0.2, with_lr=False,
            save_per_step=998, validation_split=0.2, validation_step=100, foo_save=None, validation_data=None):
        validation_accuracy, i = 0., 0
        validation_loss = 0.
        do_validate = True if validation_split > 0. else False
        X = np.copy(inputs)
        y = np.copy(targets)

        border = int(len(X) * validation_split)

        if validation_data is not None:
            X_tr, y_tr = X, y
            X_vl, y_vl = validation_data[0], validation_data[1]
        elif do_validate:
            X_tr, X_vl = X[border:], X[:border]
            y_tr, y_vl = y[border:], y[:border]
        else:
            X_tr, y_tr = X, y
            X_vl, y_vl = None, None

        total = (0 if len(X_tr) % batch_size == 0 else 1) + (len(X_tr) // batch_size)

        validation_inf = {'loss': {}, 'accuracy': {}}
        train_inf = {'loss': {}, 'accuracy': {}}

        with tf.Session() as sess:
            # try:
            sess.run([tf.global_variables_initializer(), tf.local_variables_initializer()])
            if from_chkp is not None:
                self.saver.restore(sess, self.chkp_dir + f'/{from_chkp}')

            swt = tf.summary.FileWriter(self.chkp_dir + '/train', sess.graph)
            swd = tf.summary.FileWriter(self.chkp_dir + '/dev', sess.graph)

            step = 0
            for epoch in range(epochs):
                if from_notebook:
                    progress_bar = tqdm_notebook(batch_generator(X_tr, y_tr, batch_size, to_shuffle=True),
                                                 total=total)
                else:
                    progress_bar = tqdm(batch_generator(X_tr, y_tr, batch_size, to_shuffle=True),
                                        total=total)
                for x, y_ in progress_bar:

                    weights = np.zeros_like(x)
                    weights[np.where(x != self.pad_idx)] = 1.0

                    fetches = {'train': self.train, 'accuracy': self.accuracy, 'step': self.global_step,
                               'loss': self.loss_averaged, 'summaries': self.summaries}
                    feed_dict = {self.targets: y_, self.inputs: x, self.weights: weights,
                                 self.dropout: dropout}

                    if with_lr:
                        feed_dict.update({self.lr: with_lr})

                    fetched = sess.run(fetches=fetches, feed_dict=feed_dict)

                    step = fetched['step']
                    accuracy = fetched['accuracy'] * 100
                    loss = fetched['loss']
                    summaries = fetched['summaries']
                    swt.add_summary(summaries, step)

                    train_inf['loss'][step] = loss
                    train_inf['accuracy'][step] = accuracy

                    if do_validate and (step % validation_step + 1 == validation_step):
                        val_inf = self.validate(X_vl, y_vl, sess)
                        validation_inf['loss'][step] = val_inf['loss']
                        validation_inf['accuracy'][step] = val_inf['accuracy']
                        validation_accuracy = val_inf['accuracy']
                        validation_loss = val_inf['loss']
                        summary_1 = tf.Summary(value=[
                            tf.Summary.Value(tag="loss__", simple_value=validation_loss),
                        ])
                        swd.add_summary(summary_1, step)
                        summary_2 = tf.Summary(value=[
                            tf.Summary.Value(tag="accuracy__", simple_value=validation_accuracy),
                        ])
                        swd.add_summary(summary_2, step)

                    progress_bar.set_postfix_str(
                        f'ep: {epoch + 1}/{epochs},'
                        f'loss: {"%.4f" % loss}, acc: {"%.3f" % accuracy},'
                        f'val_loss: {"%.4f" % validation_loss}, val_acc: {"%.3f" % validation_accuracy}')

                    if step % save_per_step + 1 == save_per_step:
                        self.saver.save(sess, self.chkp_dir + '/my_model',
                                        global_step=step)
                    if foo_save is not None:
                        foo_save()
            self.saver.save(sess, self.chkp_dir + '/my_model',
                            global_step=step)
        # except:
        #     print(f'saving checkpoint to {self.chkp_dir + "/my_model_interrupted"}')
        #     self.saver.save(sess, self.chkp_dir + '/my_model_interrupted')

        return train_inf, validation_inf

    def predict(self, X, batch_size, from_chkp):
        predictions = []
        with tf.Session() as sess:
            sess.run([tf.global_variables_initializer()])
            self.saver.restore(sess, self.chkp_dir + f'/{from_chkp}')
            for x in batch_generator(X, bs=batch_size):
                batch_predictions = sess.run(fetches={'predictions': self.predictions},
                                             feed_dict={self.inputs: x, self.dropout: 0.0})
                predictions.append(batch_predictions['predictions'])
        return np.concatenate(predictions, axis=0)

    def validate(self, X, y, sess):
        fetches = {'loss': self.loss, 'accuracy': self.accuracy}
        loss, accuracy, i = 0.0, 0.0, 0
        for x, y_ in batch_generator(X, y, 100):
            i += 1
            weights = np.zeros_like(x)
            weights[np.where(x != self.pad_idx)] = 1.0

            feed_dict = {self.inputs: x, self.targets: y_, self.dropout: 0.0,
                         self.weights: weights}
            fetched = sess.run(fetches, feed_dict=feed_dict)
            loss += sum(fetched['loss'])
            accuracy += fetched['accuracy']
        loss /= len(X)
        accuracy /= i
        return {'loss': loss, 'accuracy': accuracy * 100}

    @staticmethod
    def save_predictions(filename, predictions, df, vocabulary):
        voc = dict([(int(idx), cat) for cat, idx in vocabulary.items()])
        with open(filename, 'w') as f:
            for i in range(len(df)):
                tokens = df.iloc[i].tokens
                IDs = df.iloc[i].IDs
                predicts = predictions[i]
                l = len(predicts)
                predicts = [x for x in predicts] + [305] * (len(tokens) - l)
                for token, predict, id in zip(tokens, predicts, IDs):
                    if token == '_pad_': break
                    if predict not in voc:
                        cat = '_unk_#_unk_'
                    else:
                        cat = voc[predict]
                    if cat == '_pad_#_pad_':
                        cat = 'NOUN#_'
                    pos = cat.split('#')[0]
                    gram_cats = cat.split('#')[1]
                    print(id, token, '_', pos, gram_cats, sep='\t', file=f)
                print(file=f)


def batch_generator(X, y=None, bs=30, to_shuffle=False):
    extra_ep = 0 if len(X) % bs == 0 else 1
    ep_size = (len(X) // bs) + extra_ep

    A = np.copy(X)
    b = None
    if y is not None: b = np.copy(y)
    if to_shuffle:
        idxs = shuffle(np.arange(len(X)), random_state=2019)
        A = A[idxs]
        b = b[idxs]
    for i in range(ep_size):
        if y is not None:
            yield A[i * bs: (i + 1) * bs], b[i * bs: (i + 1) * bs]
        else:
            yield A[i * bs: (i + 1) * bs]


def main(from_notebook=True):
    p = argparse.ArgumentParser()
    p.add_argument('--max_seq_len', type=int, default=60)
    p.add_argument('--bs', type=int, default=50)
    p.add_argument('--optimizer', type=str, default='adam')
    p.add_argument('--hidden_size', type=int, default=150)
    p.add_argument('--emb_size', type=int, default=300)
    p.add_argument('--lr', type=float, default=0.001)
    p.add_argument('--num_layers', type=int, default=2)
    p.add_argument('--dropout', type=float, default=0.2)
    p.add_argument('--config_path', type=str, default='build_config.json')
    p.add_argument('--train_file', type=str, default='../datasets/gikrya_new_train.out')
    p.add_argument('--test_file', type=str, default='../datasets/gikrya_new_test.out')
    p.add_argument('--train_log', type=str, default='.logs')
    p.add_argument('--max_epochs', type=int, default=20)
    args = p.parse_args()

    from reader import GikryaReader

    max_seq_len = args.max_seq_len
    batch_size = args.bs
    optimizer = args.optimizer
    hidden_size = args.hidden_size
    # emb_size = args.emb_size
    lr = args.lr
    num_layers = args.num_layers
    dropout = args.dropout
    config_path = args.config_path
    max_epochs = args.max_epochs
    log = f"{datetime.now().isoformat(sep='+', timespec='minutes').replace('-', '.')}"
    print(f'logging loss and accuracy to {log}')

    reader = GikryaReader(args.train_file, pad_to=max_seq_len, min_tf=2)
    t_reader = GikryaReader(args.test_file, pad_to=max_seq_len, shuffle=False)
    num_classes = reader.num_classes
    voc_size = len(reader.vocabulary) + 1

    config = {
        'batch_size': batch_size,
        'hidden_size': hidden_size,
        'learning_rate': lr,
        'max_seq_len': max_seq_len,
        'num_classes': num_classes,
        'num_layers': num_layers,
        'optimizer': optimizer,
        'voc_size': voc_size,
        'dropout': dropout
    }

    X_train = GikryaReader.encode_sentences(reader.df, reader.vocabulary)
    y_train = GikryaReader.encode_categories_jointly(reader.df, reader.pos_gram_cats_vocabulary)
    X_train = np.array(X_train)
    y_train = np.array(y_train)

    X_test = GikryaReader.encode_sentences(t_reader.df, reader.vocabulary)
    y_test = GikryaReader.encode_categories_jointly(t_reader.df, reader.pos_gram_cats_vocabulary)
    X_test = np.array(X_test)
    y_test = np.array(y_test)

    BiLSTMClassifier.create_config(config_path, config)
    clf = BiLSTMClassifier(config_path)
    inf = clf.fit(X_train, y_train, batch_size=batch_size, epochs=max_epochs, dropout=dropout, save_per_step=2000,
                  validation_step=200, validation_data=(X_test, y_test))

    with open(log + '_train.json', 'w') as f:
        json.dump(inf[0], f, indent=4)
    with open(log + '_validation.json', 'w') as f:
        json.dump(inf[1], f, indent=4)


if __name__ == '__main__':
    from_notebook = False
    main(from_notebook)
