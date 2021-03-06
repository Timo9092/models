# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""MobileBERT embedding and transformer layers."""
import tensorflow as tf

from official.nlp import keras_nlp


class NoNorm(tf.keras.layers.Layer):
  """Apply element-wise linear transformation to the last dimension."""

  def __init__(self, name=None):
    super(NoNorm, self).__init__(name=name)

  def build(self, shape):
    kernal_size = shape[-1]
    self.bias = self.add_weight('beta',
                                shape=[kernal_size],
                                initializer='zeros')
    self.scale = self.add_weight('gamma',
                                 shape=[kernal_size],
                                 initializer='ones')

  def call(self, feature):
    output = feature * self.scale + self.bias
    return output


def _get_norm_layer(normalization_type='no_norm', name=None):
  """Get normlization layer.

  Arguments:
      normalization_type: String. The type of normalization_type, only
        'no_norm' and 'layer_norm' are supported.
      name: Name for the norm layer.

  Returns:
    layer norm class.
  """
  if normalization_type == 'no_norm':
    layer = NoNorm(name=name)
  elif normalization_type == 'layer_norm':
    layer = tf.keras.layers.LayerNormalization(
        name=name,
        axis=-1,
        epsilon=1e-12,
        dtype=tf.float32)
  else:
    raise NotImplementedError('Only "no_norm" and "layer_norm" and supported.')
  return layer


class MobileBertEmbedding(tf.keras.layers.Layer):
  """Performs an embedding lookup for MobileBERT.

  This layer includes word embedding, token type embedding, position embedding.
  """

  def __init__(self,
               word_vocab_size,
               word_embed_size,
               type_vocab_size,
               output_embed_size,
               max_sequence_length=512,
               normalization_type='no_norm',
               initializer=tf.keras.initializers.TruncatedNormal(stddev=0.02),
               dropout_rate=0.1,
               **kwargs):
    """Class initialization.

    Arguments:
      word_vocab_size: Number of words in the vocabulary.
      word_embed_size: Word embedding size.
      type_vocab_size: Number of word types.
      output_embed_size: Embedding size for the final embedding output.
      max_sequence_length: Maximum length of input sequence.
      normalization_type: String. The type of normalization_type, only
        'no_norm' and 'layer_norm' are supported.
      initializer: The initializer to use for the embedding weights and
        linear projection weights.
      dropout_rate: Dropout rate.
      **kwargs: keyword arguments.
    """
    super(MobileBertEmbedding, self).__init__(**kwargs)
    self.word_vocab_size = word_vocab_size
    self.word_embed_size = word_embed_size
    self.type_vocab_size = type_vocab_size
    self.output_embed_size = output_embed_size
    self.max_sequence_length = max_sequence_length
    self.normalization_type = normalization_type
    self.initializer = tf.keras.initializers.get(initializer)
    self.dropout_rate = dropout_rate

    self.word_embedding = keras_nlp.layers.OnDeviceEmbedding(
        self.word_vocab_size,
        self.word_embed_size,
        initializer=initializer,
        name='word_embedding')
    self.type_embedding = keras_nlp.layers.OnDeviceEmbedding(
        self.type_vocab_size,
        self.output_embed_size,
        use_one_hot=True,
        initializer=initializer,
        name='type_embedding')
    self.pos_embedding = keras_nlp.layers.PositionEmbedding(
        max_length=max_sequence_length,
        initializer=initializer,
        name='position_embedding')
    self.word_embedding_proj = tf.keras.layers.experimental.EinsumDense(
        'abc,cd->abd',
        output_shape=[None, self.output_embed_size],
        kernel_initializer=initializer,
        bias_axes='d',
        name='embedding_projection')
    self.layer_norm = _get_norm_layer(normalization_type, 'embedding_norm')
    self.dropout_layer = tf.keras.layers.Dropout(
        self.dropout_rate,
        name='embedding_dropout')

  def get_config(self):
    config = {
        'word_vocab_size': self.word_vocab_size,
        'word_embed_size': self.word_embed_size,
        'type_vocab_size': self.type_vocab_size,
        'output_embed_size': self.output_embed_size,
        'max_sequence_length': self.max_sequence_length,
        'normalization_type': self.normalization_type,
        'initializer': tf.keras.initializers.serialize(self.initializer),
        'dropout_rate': self.dropout_rate
    }
    base_config = super(MobileBertEmbedding, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self, input_ids, token_type_ids=None):
    word_embedding_out = self.word_embedding(input_ids)
    word_embedding_out = tf.concat(
        [tf.pad(word_embedding_out[:, 1:], ((0, 0), (0, 1), (0, 0))),
         word_embedding_out,
         tf.pad(word_embedding_out[:, :-1], ((0, 0), (1, 0), (0, 0)))],
        axis=2)
    word_embedding_out = self.word_embedding_proj(word_embedding_out)

    pos_embedding_out = self.pos_embedding(word_embedding_out)
    embedding_out = word_embedding_out + pos_embedding_out
    if token_type_ids is not None:
      type_embedding_out = self.type_embedding(token_type_ids)
      embedding_out += type_embedding_out
    embedding_out = self.layer_norm(embedding_out)
    embedding_out = self.dropout_layer(embedding_out)

    return embedding_out


class MobileBertTransformer(tf.keras.layers.Layer):
  """Transformer block for MobileBERT.

  An implementation of one layer (block) of Transformer with bottleneck and
  inverted-bottleneck for MobilerBERT.

  Original paper for MobileBERT:
  https://arxiv.org/pdf/2004.02984.pdf
  """

  def __init__(self,
               hidden_size=512,
               num_attention_heads=4,
               intermediate_size=512,
               intermediate_act_fn='relu',
               hidden_dropout_prob=0.1,
               attention_probs_dropout_prob=0.1,
               intra_bottleneck_size=128,
               use_bottleneck_attention=False,
               key_query_shared_bottleneck=True,
               num_feedforward_networks=4,
               normalization_type='no_norm',
               initializer=tf.keras.initializers.TruncatedNormal(stddev=0.02),
               **kwargs):
    """Class initialization.

    Arguments:
      hidden_size: Hidden size for the Transformer input and output tensor.
      num_attention_heads: Number of attention heads in the Transformer.
      intermediate_size: The size of the "intermediate" (a.k.a., feed
        forward) layer.
      intermediate_act_fn: The non-linear activation function to apply
        to the output of the intermediate/feed-forward layer.
      hidden_dropout_prob: Dropout probability for the hidden layers.
      attention_probs_dropout_prob: Dropout probability of the attention
        probabilities.
      intra_bottleneck_size: Size of bottleneck.
      use_bottleneck_attention: Use attention inputs from the bottleneck
        transformation. If true, the following `key_query_shared_bottleneck`
        will be ignored.
      key_query_shared_bottleneck: Whether to share linear transformation for
        keys and queries.
      num_feedforward_networks: Number of stacked feed-forward networks.
      normalization_type: The type of normalization_type, only 'no_norm' and
        'layer_norm' are supported. 'no_norm' represents the element-wise
        linear transformation for the student model, as suggested by the
        original MobileBERT paper. 'layer_norm' is used for the teacher model.
      initializer: The initializer to use for the embedding weights and
        linear projection weights.
      **kwargs: keyword arguments.

    Raises:
      ValueError: A Tensor shape or parameter is invalid.
    """
    super(MobileBertTransformer, self).__init__(**kwargs)
    self.hidden_size = hidden_size
    self.num_attention_heads = num_attention_heads
    self.intermediate_size = intermediate_size
    self.intermediate_act_fn = intermediate_act_fn
    self.hidden_dropout_prob = hidden_dropout_prob
    self.attention_probs_dropout_prob = attention_probs_dropout_prob
    self.intra_bottleneck_size = intra_bottleneck_size
    self.use_bottleneck_attention = use_bottleneck_attention
    self.key_query_shared_bottleneck = key_query_shared_bottleneck
    self.num_feedforward_networks = num_feedforward_networks
    self.normalization_type = normalization_type
    self.initializer = tf.keras.initializers.get(initializer)

    if intra_bottleneck_size % num_attention_heads != 0:
      raise ValueError(
          (f'The bottleneck size {intra_bottleneck_size} is not a multiple '
           f'of the number of attention heads {num_attention_heads}.'))
    attention_head_size = int(intra_bottleneck_size / num_attention_heads)

    self.block_layers = {}
    # add input bottleneck
    dense_layer_2d = tf.keras.layers.experimental.EinsumDense(
        'abc,cd->abd',
        output_shape=[None, self.intra_bottleneck_size],
        bias_axes='d',
        kernel_initializer=initializer,
        name='bottleneck_input/dense')
    layer_norm = _get_norm_layer(self.normalization_type,
                                 name='bottleneck_input/norm')
    self.block_layers['bottleneck_input'] = [dense_layer_2d,
                                             layer_norm]

    if self.key_query_shared_bottleneck:
      dense_layer_2d = tf.keras.layers.experimental.EinsumDense(
          'abc,cd->abd',
          output_shape=[None, self.intra_bottleneck_size],
          bias_axes='d',
          kernel_initializer=initializer,
          name='kq_shared_bottleneck/dense')
      layer_norm = _get_norm_layer(self.normalization_type,
                                   name='kq_shared_bottleneck/norm')
      self.block_layers['kq_shared_bottleneck'] = [dense_layer_2d,
                                                   layer_norm]

    # add attention layer
    attention_layer = tf.keras.layers.MultiHeadAttention(
        num_heads=self.num_attention_heads,
        key_dim=attention_head_size,
        value_dim=attention_head_size,
        dropout=self.attention_probs_dropout_prob,
        output_shape=self.intra_bottleneck_size,
        kernel_initializer=initializer,
        name='attention')
    layer_norm = _get_norm_layer(self.normalization_type,
                                 name='attention/norm')
    self.block_layers['attention'] = [attention_layer,
                                      layer_norm]

    # add stacked feed-forward networks
    self.block_layers['ffn'] = []
    for ffn_layer_idx in range(self.num_feedforward_networks):
      layer_prefix = f'ffn_layer_{ffn_layer_idx}'
      layer_name = layer_prefix + '/intermediate_dense'
      intermediate_layer = tf.keras.layers.experimental.EinsumDense(
          'abc,cd->abd',
          activation=self.intermediate_act_fn,
          output_shape=[None, self.intermediate_size],
          bias_axes='d',
          kernel_initializer=initializer,
          name=layer_name)
      layer_name = layer_prefix + '/output_dense'
      output_layer = tf.keras.layers.experimental.EinsumDense(
          'abc,cd->abd',
          output_shape=[None, self.intra_bottleneck_size],
          bias_axes='d',
          kernel_initializer=initializer,
          name=layer_name)
      layer_name = layer_prefix + '/norm'
      layer_norm = _get_norm_layer(self.normalization_type,
                                   name=layer_name)
      self.block_layers['ffn'].append([intermediate_layer,
                                       output_layer,
                                       layer_norm])

    # add output bottleneck
    bottleneck = tf.keras.layers.experimental.EinsumDense(
        'abc,cd->abd',
        output_shape=[None, self.hidden_size],
        activation=None,
        bias_axes='d',
        kernel_initializer=initializer,
        name='bottleneck_output/dense')
    dropout_layer = tf.keras.layers.Dropout(
        self.hidden_dropout_prob,
        name='bottleneck_output/dropout')
    layer_norm = _get_norm_layer(self.normalization_type,
                                 name='bottleneck_output/norm')
    self.block_layers['bottleneck_output'] = [bottleneck,
                                              dropout_layer,
                                              layer_norm]

  def get_config(self):
    config = {
        'hidden_size': self.hidden_size,
        'num_attention_heads': self.num_attention_heads,
        'intermediate_size': self.intermediate_size,
        'intermediate_act_fn': self.intermediate_act_fn,
        'hidden_dropout_prob': self.hidden_dropout_prob,
        'attention_probs_dropout_prob': self.attention_probs_dropout_prob,
        'intra_bottleneck_size': self.intra_bottleneck_size,
        'use_bottleneck_attention': self.use_bottleneck_attention,
        'key_query_shared_bottleneck': self.key_query_shared_bottleneck,
        'num_feedforward_networks': self.num_feedforward_networks,
        'normalization_type': self.normalization_type,
        'initializer': tf.keras.initializers.serialize(self.initializer),
    }
    base_config = super(MobileBertTransformer, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def call(self,
           input_tensor,
           attention_mask=None,
           return_attention_scores=False):
    """Implementes the forward pass.

    Arguments:
      input_tensor: Float tensor of shape [batch_size, seq_length, hidden_size].
      attention_mask: (optional) int32 tensor of shape [batch_size, seq_length,
        seq_length], with 1 for positions that can be attended to and 0 in
        positions that should not be.
      return_attention_scores: If return attention score.

    Returns:
      layer_output: Float tensor of shape [batch_size, seq_length, hidden_size].
      attention_scores (Optional): Only when return_attention_scores is True.

    Raises:
      ValueError: A Tensor shape or parameter is invalid.
    """
    input_width = input_tensor.shape.as_list()[-1]
    if input_width != self.hidden_size:
      raise ValueError(
          (f'The width of the input tensor {input_width} != '
           f'hidden size {self.hidden_size}'))

    prev_output = input_tensor
    # input bottleneck
    dense_layer = self.block_layers['bottleneck_input'][0]
    layer_norm = self.block_layers['bottleneck_input'][1]
    layer_input = dense_layer(prev_output)
    layer_input = layer_norm(layer_input)

    if self.use_bottleneck_attention:
      key_tensor = layer_input
      query_tensor = layer_input
      value_tensor = layer_input
    elif self.key_query_shared_bottleneck:
      dense_layer = self.block_layers['kq_shared_bottleneck'][0]
      layer_norm = self.block_layers['kq_shared_bottleneck'][1]
      shared_attention_input = dense_layer(prev_output)
      shared_attention_input = layer_norm(shared_attention_input)
      key_tensor = shared_attention_input
      query_tensor = shared_attention_input
      value_tensor = prev_output
    else:
      key_tensor = prev_output
      query_tensor = prev_output
      value_tensor = prev_output

    # attention layer
    attention_layer = self.block_layers['attention'][0]
    layer_norm = self.block_layers['attention'][1]
    attention_output, attention_scores = attention_layer(
        query_tensor,
        value_tensor,
        key_tensor,
        attention_mask,
        return_attention_scores=True,
    )
    attention_output = layer_norm(attention_output + layer_input)

    # stacked feed-forward networks
    layer_input = attention_output
    for ffn_idx in range(self.num_feedforward_networks):
      intermediate_layer = self.block_layers['ffn'][ffn_idx][0]
      output_layer = self.block_layers['ffn'][ffn_idx][1]
      layer_norm = self.block_layers['ffn'][ffn_idx][2]
      intermediate_output = intermediate_layer(layer_input)
      layer_output = output_layer(intermediate_output)
      layer_output = layer_norm(layer_output + layer_input)
      layer_input = layer_output

    # output bottleneck
    bottleneck = self.block_layers['bottleneck_output'][0]
    dropout_layer = self.block_layers['bottleneck_output'][1]
    layer_norm = self.block_layers['bottleneck_output'][2]
    layer_output = bottleneck(layer_output)
    layer_output = dropout_layer(layer_output)
    layer_output = layer_norm(layer_output + prev_output)

    if return_attention_scores:
      return layer_output, attention_scores
    else:
      return layer_output
