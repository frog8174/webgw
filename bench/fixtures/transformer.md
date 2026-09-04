35 languages
  * [Afrikaans](https://af.wikipedia.org/wiki/Transformator "Transformator – Afrikaans")
  * [العربية](https://ar.wikipedia.org/wiki/x "Arabic")
  * [Bahasa Indonesia](https://id.wikipedia.org/wiki/x "Indonesian")
  * [Deutsch](https://de.wikipedia.org/wiki/x "German")
  * [Español](https://es.wikipedia.org/wiki/x "Spanish")
  * [Français](https://fr.wikipedia.org/wiki/x "French")
  * [日本語](https://ja.wikipedia.org/wiki/x "Japanese")

[Edit links](https://www.wikidata.org/wiki/x "Edit interlanguage links")
  * [Article](https://en.wikipedia.org/wiki/Transformer "View the content page")
  * [Talk](https://en.wikipedia.org/wiki/Talk:Transformer "Discuss improvements")
  * [Read](https://en.wikipedia.org/wiki/Transformer)
  * [Edit](https://en.wikipedia.org/w/index.php?title=Transformer&action=edit)
  * [View history](https://en.wikipedia.org/w/index.php?title=Transformer&action=history)

Tools
  * [What links here](https://en.wikipedia.org/wiki/Special:WhatLinksHere/Transformer)
  * [Related changes](https://en.wikipedia.org/wiki/Special:RecentChangesLinked/Transformer)
  * [Upload file](https://en.wikipedia.org/wiki/Wikipedia:File_Upload_Wizard)
  * [Permanent link](https://en.wikipedia.org/w/index.php?title=Transformer&oldid=1)
  * [Page information](https://en.wikipedia.org/w/index.php?title=Transformer&action=info)
  * [Cite this page](https://en.wikipedia.org/w/index.php?title=Special:CiteThisPage)
  * [Download as PDF](https://en.wikipedia.org/w/index.php?title=Special:DownloadAsPdf)
  * [Printable version](https://en.wikipedia.org/w/index.php?title=Transformer&printable=yes)

## Attention head

[[edit](https://en.wikipedia.org/w/index.php?title=Transformer&action=edit&section=18 "Edit section: Attention head")]
The attention mechanism used in the transformer architecture are scaled dot-product
attention units. For each attention unit, the transformer model learns three weight
matrices: the query weights, the key weights, and the value weights. Each token
produces a query vector, a key vector and a value vector by multiplying the token
embedding against these matrices.

Attention weights are calculated using the query and key vectors: the attention
weight from token i to token j is the dot product between the query vector of
token i and the key vector of token j. The dot products are divided by the square
root of the dimension of the key vectors, which stabilizes gradients during
training, and passed through a softmax which normalizes the weights so they sum
to one. The output of the attention unit for token i is the weighted sum of the
value vectors of all tokens, weighted by the attention weights from token i.

The computation for all tokens can be expressed as one large matrix calculation
using the softmax function, which is useful for training because computational
matrix operations run quickly in parallel on tensor hardware.

## Multihead attention

[[edit](https://en.wikipedia.org/w/index.php?title=Transformer&action=edit&section=19 "Edit section: Multihead attention")]
One set of query, key and value weight matrices is called an attention head, and
each layer in a transformer model has multiple attention heads. While each
attention head attends to the tokens that are relevant to each token, multiple
attention heads allow the model to do this for different definitions of
"relevance". The computations for each attention head can be performed in
parallel, which allows for fast processing.

The outputs of all the attention heads in one layer are concatenated and passed
through the feed-forward neural network layers. Concatenating the head outputs
and projecting them with a further learned matrix is what multi-head attention
means in practice.

## Subsequent work

[[edit](https://en.wikipedia.org/w/index.php?title=Transformer&action=edit&section=27 "Edit section: Subsequent work")]

## Tokenization

[[edit](https://en.wikipedia.org/w/index.php?title=Transformer&action=edit&section=11 "Edit section: Tokenization")]
As the transformer architecture natively consists of operations over numbers
rather than over text, there must be a way to convert text into numbers. Each
text is converted into a sequence of tokens by a tokenizer, and each token is
converted into a numerical vector by looking it up in a word embedding table.
Byte pair encoding is a common tokenization scheme that builds a vocabulary of
subword units from a training corpus.

## Parallelizing attention

[[edit](https://en.wikipedia.org/w/index.php?title=Transformer&action=edit&section=4 "Edit section: Parallelizing attention")]
Before transformers, sequence models processed tokens one at a time, which made
training slow. The transformer removes the recurrence entirely, so every position
in the sequence can be processed simultaneously. This parallelism is the main
reason transformers scale to large corpora where recurrent models did not.
