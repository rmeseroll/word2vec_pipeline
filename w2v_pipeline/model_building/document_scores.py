import collections, itertools, os
import numpy as np
import pandas as pd
import h5py

from gensim.models.word2vec import Word2Vec
from mapreduce import simple_mapreduce

#import multiprocessing_on_dill
#exit()



class document_scores(simple_mapreduce):

    def set_iterator_function(self, iter_func):
        self.iter_func = iter_func

    def sentence_iterator(self):
        for item in self.iter_func():
            text,idx,f_sql = item
            yield text.split()

    def __init__(self,*args,**kwargs):
        super(document_scores, self).__init__(*args,**kwargs)

         # Load the model from disk
        self.M = Word2Vec.load(kwargs["f_w2v"])       
        self.shape = self.M.syn0.shape
        
        # Build total counts
        self.counts = {}
        for key,val in self.M.vocab.items():
            self.counts[key] = val.count

        # Build the dictionary
        self.methods = kwargs["methods"]
        vocab_n = self.shape[0]
        self.word2index = dict(zip(self.M.index2word,range(vocab_n)))

        # Set parallel option
        self._PARALLEL = kwargs["_PARALLEL"]

    def score_document(self, item):

        text,idx,f_sql = item
        tokens = text.split()

        # Find out which tokens are defined
        valid_tokens = [w for w in tokens if w in self.M]
        local_counts = collections.Counter(valid_tokens)
        tokens = set(valid_tokens)
        method = self.current_method

        if not tokens:
            msg = "Document has no valid tokens! This is problem."
            raise ValueError(msg)

        # Lookup the weights (model dependent)
        if method in ["unique"]:
            weights = dict.fromkeys(tokens, 1.0)
        elif method in ["simple"]:
            weights = dict([(w,local_counts[w]) for w in tokens])
        elif method in ["TF_IDF","kSVD"]:
            weights = dict([(w,IDF[w]*c) 
                            for w,c in local_counts.items()])
        else:
            msg = "UNKNOWN w2v method '{}'".format(method)
            raise KeyError(msg)

        # Lookup the embedding vector
        if method in ["unique","simple","TF_IDF"]:
            DV = [self.M[w] for w in tokens]
        elif method in ["kSVD"]:
            word_idx = [self.word2index[w] for w in tokens]
            DV = [self.kSVD_gamma[n] for n in word_idx]
        else:
            msg = "UNKNOWN w2v method '{}'".format(method)
            raise KeyError(msg)

        # Build the weight matrix
        W  = np.array([weights[w] for w in tokens]).reshape(-1,1)
        DV = np.array(DV)

        # Sum all the vectors with their weights
        doc_vec = (W*DV).sum(axis=0)        
        
        # Renormalize onto the hypersphere
        doc_vec /= np.linalg.norm(doc_vec)

        # Sanity check, L1 norm
        assert(np.isclose(1.0, np.linalg.norm(doc_vec)))

        return doc_vec,idx,f_sql

    def compute(self, config):
        '''
        if self._PARALLEL:
            import multiprocessing
            MP = multiprocessing.Pool()
            ITR = MP.imap(self.score_document, self.iter_func())
        else:
            ITR = itertools.imap(self.score_document, self.iter_func())
        '''

        for self.current_method in self.methods:
            print "Scoring {}".format(self.current_method)
                    
            ITR = itertools.imap(self.score_document, self.iter_func())
            data = []
            for k,result in enumerate(ITR):
                data.append(result)

            self.df = pd.DataFrame(data=data,
                                   columns=["V","idx","f_sql"])      

    def save(self, config):

        method = self.current_method

        print "Saving the scored documents"       
        f_db = config["document_scores"]["f_db"]
        if not os.path.exists(f_db):
            h5 = h5py.File(f_db,'w')
        else:
            h5 = h5py.File(f_db,'r+')

        for key,V in self.df.groupby("f_sql"):
            
            g  = h5.require_group(key)

            V = np.array(self.df.sort_values("idx")["V"].tolist())

            if method in g:
                del g[method]

            g.create_dataset(method,
                             data=V,
                             compression='gzip')

        h5.close()
