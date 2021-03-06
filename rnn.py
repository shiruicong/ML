import tensorflow as tf
import os

## seq2seq版本，不加attention机制 ##

MAX_LEN = 50
SOS_ID = 0

SRC_TRAIN_DATA = "data/en_train_ids.txt"  # 源语言输入文件
TRG_TRAIN_DATA = "data/cn_train_ids.txt"  # 目标语言输入文件
CHECKPOINT_PATH = "checkpoint/rnn/rnn"
HIDDEN_SIZE = 1024  # LSTM的隐藏层规模
NUM_LAYERS = 2   # 深层循环神经网络中LSTM结构的层数
SRC_VOCAB_SIZE = 11872  # 源语言词汇表大小
TRG_VOCAB_SIZE = 13289  # 目标语言词汇表大小
BATCH_SIZE = 64  # 训练数据的batch的大小
NUM_EPOCH = 5  # 使用训练数据的轮数
KEEP_PROB = 0.8  # 节点不被dropout的概率
MAX_GRAD_NORM = 5  # 用于控制梯度膨胀的梯度大小上限
SHARE_EMB_AND_SOFTMAX= True  # 在softmax层和词向量之间共享参数
LEARNING_RATE = 1.0

if not os.path.exists(CHECKPOINT_PATH):
    os.mkdir(CHECKPOINT_PATH)

# 使用Dataset从一个文件中读取一个语言的数据
def Makedataset(file_path):
    dataset = tf.data.TextLineDataset(file_path)
    # 根据空格将单词编号切分开并放入一个一维向量
    dataset = dataset.map(lambda string: tf.string_split([string]).values)
    # 将字符串形式的单词编号转化为整数
    dataset = dataset.map(lambda string: tf.string_to_number(string, tf.int32))
    # 统计每个单词的单词数量，并与句子内容一起放入Dataset中
    dataset = dataset.map(lambda x: (x, tf.size(x)))
    return dataset


# 从源语言文件src_path和目标语言文件trg_path中分别读取数据，并进行填充和batching操作
def MakeSrcTrgDataset(src_path, trg_path, batch_size):
    # 首先分别读取源语言数据和目标语言数据
    src_data = Makedataset(src_path)
    trg_data = Makedataset(trg_path)
    # 通过zip操作将两个Dataset合并为一个Dataset，现在每个Dataset中的每一项数据dataset由四个张量组成
    # dataset[0][0] 是源句子
    # dataset[0][1] 是源句子长度
    # dataset[1][0] 是目标句子
    # dataset[1][1] 是目标句子长度
    dataset = tf.data.Dataset.zip((src_data, trg_data))

    # 删除内容为空（只包含<eos>）的句子和长度过长的句子
    def FilterLength(src_tuple, trg_tuple):
        ((src_input, src_len), (trg_label, trg_len)) = (src_tuple, trg_tuple)
        src_len_ok = tf.logical_and(tf.greater(src_len, 1), tf.less_equal(src_len, MAX_LEN))
        trg_len_ok = tf.logical_and(tf.greater(trg_len, 1), tf.less_equal(trg_len, MAX_LEN))
        return tf.logical_and(src_len_ok, trg_len_ok)
    dataset = dataset.filter(FilterLength)

    # trg_input比trg_label多了开始符SOS
    def MakeTrgInput(src_tuple, trg_tuple):
        ((src_input, src_len), (trg_label, trg_len)) = (src_tuple, trg_tuple)
        trg_input = tf.concat([[SOS_ID], trg_label[:-1]], axis=0)
        return (src_input, src_len), (trg_input, trg_label, trg_len)
    dataset = dataset.map(MakeTrgInput)

    # 随机打乱数据
    dataset = dataset.shuffle(10000)

    # 规定填充后输出的数据维度
    padded_shapes = (
        (tf.TensorShape([None]),  # 源句子是长度未知的向量
         tf.TensorShape([])),  # 源句子长度是单个数字
        (tf.TensorShape([None]),  # 目标句子（解码器输入）是长度未知的向量
         tf.TensorShape([None]),  # 目标句子（解码器目标输出）是长度未知的向量
         tf.TensorShape([])))   # 目标句子长度是单个数字
    # 调用padded_batch方法进行batching操作
    batched_dataset = dataset.padded_batch(batch_size, padded_shapes)
    return batched_dataset


class NMTModel(object):
    # 在模型的初始化函数中定义模型要用到的变量
    def __init__(self):
        # 定义编码器和解码器所使用的LSTM结构
        # 堆叠双层RNN
        self.enc_cell = tf.nn.rnn_cell.MultiRNNCell(
            [tf.nn.rnn_cell.BasicLSTMCell(HIDDEN_SIZE) for _ in range(NUM_LAYERS)])
        self.dec_cell = tf.nn.rnn_cell.MultiRNNCell(
            [tf.nn.rnn_cell.BasicLSTMCell(HIDDEN_SIZE) for _ in range(NUM_LAYERS)])

        # 为源语言和目标语言分别定义词向量
        # 获取已存在的变量（要求不仅名字，而且初始化方法等各个参数都一样），如果不存在，就新建一个
        # 方便共享变量
        self.src_embedding = tf.get_variable("src_emb", [SRC_VOCAB_SIZE, HIDDEN_SIZE])
        self.trg_embedding = tf.get_variable("trg_emb", [TRG_VOCAB_SIZE, HIDDEN_SIZE])

        # 定义softmax层的变量
        if SHARE_EMB_AND_SOFTMAX:
            self.softmax_weight = tf.transpose(self.trg_embedding)
        else:
            self.softmax_weight = tf.get_variable("weight", [HIDDEN_SIZE, TRG_VOCAB_SIZE])  # 隐层size
        self.softmax_bias = tf.get_variable("softmax_bias", [TRG_VOCAB_SIZE])

    # 在forward函数中定义模型的前向计算图
    # src_input,src_size,trg_input,trg_label,trg_size分别是上面MakeSrcTrgDataset函数产生的五种张量
    def forward(self, src_input, src_size, trg_input, trg_label, trg_size):
        batch_size = tf.shape(src_input)[0]
        # 将输入和输出单词编号转为词向量，形如[1 0 1 0 0 0]
        src_emb = tf.nn.embedding_lookup(self.src_embedding, src_input)
        trg_emb = tf.nn.embedding_lookup(self.trg_embedding, trg_input)

        # 在词向量上进行dropout
        src_emb = tf.nn.dropout(src_emb, KEEP_PROB)
        trg_emb = tf.nn.dropout(trg_emb, KEEP_PROB)

        # 使用dynamic_rnn构造编码器
        # 编码器读取源句子每个位置的词向量，输出最后一步的隐藏状态enc_state
        # 因为编码器是一个双层LSTM，因此enc_state是一个包含两个LSTMStateTuple类的tuple，
        # 每个LSTMStateTuple对应编码器中一层的状态
        # enc_outputs是顶层LSTM在每一步的输出，它的维度是[batch_size, max_time, HIDDEN_SIZE]
        # seq2seq模型中不会用到enc_outputs，而attention模型会用到它
        with tf.variable_scope("encoder"):
            enc_outputs, enc_state = tf.nn.dynamic_rnn(self.enc_cell, src_emb, src_size, dtype=tf.float32)

        # 使用dynamic_rnn构造解码器
        # 解码器读取目标句子每个位置的词向量，输出的dec_outputs为每一步顶层LSTM的输出，dec_outputs的维度是
        # [batch_size, max_time, HIDDEN_SIZE]
        # initial_state=enc_state表示用编码器的输出来初始化第一步的隐藏状态
        with tf.variable_scope("decoder"):
            dec_outputs, _ = tf.nn.dynamic_rnn(self.dec_cell, trg_emb, trg_size, initial_state=enc_state)

        # 计算解码器每一步的log perplexity
        output = tf.reshape(dec_outputs, [-1, HIDDEN_SIZE])
        logits = tf.matmul(output, self.softmax_weight) + self.softmax_bias
        loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=tf.reshape(trg_label, [-1]), logits=logits)

        # 在计算平均损失时，需要将填充位置的权重设置为0，以避免无效位置的预测干扰
        label_weights = tf.sequence_mask(trg_size, maxlen=tf.shape(trg_label)[1], dtype=tf.float32)
        label_weights = tf.reshape(label_weights, [-1])
        cost = tf.reduce_sum(loss * label_weights)
        cost_per_token = cost / tf.reduce_sum(label_weights)

        # 定义方向传播
        # get需要训练的变量列表
        trainable_variables = tf.trainable_variables()

        # 控制梯度大小，定义优化方法和训练步骤
        grads = tf.gradients(cost / tf.to_float(batch_size), trainable_variables)
        # 控制梯度在一定范围内
        grads, _ = tf.clip_by_global_norm(grads, MAX_GRAD_NORM)
        optimizer = tf.train.GradientDescentOptimizer(learning_rate=LEARNING_RATE)
        train_op = optimizer.apply_gradients(zip(grads, trainable_variables))
        return cost_per_token, train_op


def run_epoch(session, cost_op, train_op, saver, step):
    while True:
        try:
            cost, _ = session.run([cost_op, train_op])
            if step % 10 == 0:
                print("After %d steps, per token cost is %.3f" % (step, cost))
            if step % 200 == 0:
                saver.save(session, CHECKPOINT_PATH, global_step=step)
            step += 1
        except tf.errors.OutOfRangeError:
            break
    return step


def main():
    # 定义初始化函数
    initializer = tf.random_uniform_initializer(-0.05, 0.05)

    # 定义网络
    with tf.variable_scope("nmt_model", reuse=None, initializer=initializer):
        train_model = NMTModel()

    # 定义输入数据
    data = MakeSrcTrgDataset(SRC_TRAIN_DATA, TRG_TRAIN_DATA, BATCH_SIZE)
    iterator = data.make_initializable_iterator()
    (src, src_size), (trg_input, trg_label, trg_size) = iterator.get_next()

    # 定义前向计算图，输入数据以张量形式提供给forward
    cost_op, train_op = train_model.forward(src, src_size, trg_input, trg_label, trg_size)

    # 训练模型
    saver = tf.train.Saver()
    step = 0
    with tf.Session() as sess:
        tf.global_variables_initializer().run()
        for i in range(NUM_EPOCH):
            print("In iteration:%d" % (i + 1))
            sess.run(iterator.initializer)
            step = run_epoch(sess, cost_op, train_op, saver, step)


if __name__ == "__main__":
    main()


