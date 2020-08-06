import subprocess, time, select, os, uuid, warnings
import numpy as np
import pandas as pd
from collections import defaultdict
import matplotlib.pyplot as plt
import networkx as nx
import copy
import warnings
from pygraphviz import AGraph
from graphviz import Digraph, Source
from collections.abc import Iterable
import tqdm
import joblib
import dill
import multiprocessing as mp

def multiprocessing_setup():
    if mp.get_start_method(allow_none=True) != "spawn":
        if mp.get_start_method(allow_none=True) is not None:
            warnings.warn("cannot set multiprocessing to spawn, \
            use ParallelCreditFlow with caution")
        else:
            # important for not deadlock with numpy for using dill
            mp.set_start_method("spawn")

    dill.settings['recurse'] = True # important for reloading with dill
    
multiprocessing_setup()

class GraphIterator:
    def __init__(self, graph):
        self._graph = graph
        self._index = 0 # keep track of current index

    def __next__(self):
        '''returns next node'''
        if self._index < len(self._graph):
            result = self._graph.nodes[self._index]
            self._index += 1
            return result

        # end of iteration
        raise StopIteration

class Graph:
    '''list of nodes'''
    def __init__(self, nodes, baseline_sampler={}, target_sampler={},
                 display_translator={}):
        '''
        nodes: sequence of Node object
        baseline_sampler: {name: (lambda: val)} where name
                          point to a node in nodes
        target_sampler: {name: (lambda: val)} where name 
                        point to a node in nodes
                        gives the current value
                        of the explanation instance; it can be
                        stochastic when noise of target is not observed
        display_translator: {name: lamda val: translated val}
                            translate value to human readable
        '''
        self.nodes = list(set(nodes))
        self.baseline_sampler = baseline_sampler
        self.target_sampler = target_sampler
        self.display_translator = defaultdict(lambda: (lambda x: x) )
        for k, v in display_translator.items():
            self.display_translator[k] = v

    def __len__(self):
        return len(self.nodes)

    def __iter__(self):
        return GraphIterator(self)

    def to_graphviz(self):
        '''
        convert to graphviz format
        '''
        G = AGraph(directed=True)
        for node1 in topo_sort(self.nodes):
            for node2 in node1.args:

                for node in [node1, node2]:
                    if node not in G:
                        G.add_node(node, label=node.name)

                G.add_edge(node2, node1)
                
        return G

    def draw(self):
        '''
        requires in ipython notebook environment
        '''
        viz_graph(self.to_graphviz())
        
    def add_node(self, node):
        '''
        add a node to nodes
        '''
        self.nodes.append(node)
        
    def sample(self, sampler, name):
        '''
        sample from a sampler
        if the sampled data is not an iterable, make it so
        '''
        # todo: prefetch this and also just reuse the same bg for all targets
        s = sampler[name]()
        if not isinstance(s, Iterable):
            s = [s]
        return np.array(s)
    
    def sample_all(self, sampler):
        '''
        sampler could be baseline sampler or target sampler
        return {name: val} where val is a numpy array
        '''
        d = {}
        for name in sampler:
            d[name] = self.sample(sampler, name)
        return d
            
    def reset(self):

        baseline_values = self.sample_all(self.baseline_sampler)
        target_values = self.sample_all(self.target_sampler)

        n_targets = 0
        for node in topo_sort(self):
            if len(node.args) == 0: # source node
                node.set_baseline_target(baseline_values[node.name],
                                         target_values[node.name])
            else:
                computed_baseline = node.f(*[arg.baseline for arg in node.args])
                computed_target = node.f(*[arg.target for arg in node.args])
                node.set_baseline_target(computed_baseline,
                                         computed_target)

            for c in node.children: # only activated edges change visibility
                c.visible_arg_values[node] = node.baseline

            n_targets += node.is_target_node

        assert n_targets == 1, f"{n_targets} target node, need 1"

class Node:
    '''models feature node as a computing function'''
    def __init__(self, name, f=None, args=[],
                 is_target_node=False, is_noise_node=False,
                 is_dummy_node=False,
                 is_categorical=False):
        '''
        name: name of the node
        f: functional form of this variable on other variables
        args: arguments node, predessors of the node
        children: children of the node
        target: current value
        baseline: baseline value
        is_target_node: is this node to explain
        is_noise_node: is this node a noise node
        is_dummy_node: is this a dummy node 
                       a dummy node is a node that has one parent and
                       one children that shouldn't be drawn
                       but are in place to support multigraph and
                       graph folding
        is_categorical: is the variable categorical
        '''
        self.name = name
        self.f = f
        self.is_target_node = is_target_node
        self.is_noise_node = is_noise_node
        self.is_dummy_node = is_dummy_node
        self.is_categorical = is_categorical

        # arg values that are visible to the node
        self.visible_arg_values = {}
        
        self.args = []
        for arg in args:
            self.add_arg(arg)
            
        self.children = [] # inferred from args
        
    def set_baseline_target(self, baseline, target):
        self.target = target
        self.baseline = baseline

        # reset value
        self.last_val = self.baseline
        self.val = self.baseline
        self.from_node = None
        
    def add_arg(self, node):
        '''add predecessor, allow multi edge'''
        self.args.append(node)
        node.children.append(self)            
        
    def __repr__(self):
        return self.name

class CreditFlow:
    ''' the main algorithm to get a flow of information '''
    def __init__(self, graph, verbose=False, nruns=10,
                 visualize=False, fold_noise=True,
                 silent=False):
        ''' 
        graph: causal graph to explain
        verbose: whether to print out decision process        
        nruns: number of sampled valid timelines and permutations
        visualize: whether to visualize the graph build process, 
                   need to be verbose
        fold_noise: whether to show noise node as a point
        silent: if true, overwrite visualize and verbose to False
                and does not show progress bar
        '''
        self.graph = graph
        self.edge_credit = defaultdict(lambda: defaultdict(int))
        self.verbose = verbose
        self.nruns = nruns
        self.visualize = visualize
        self.penwidth_stress = 5
        self.penwidth_normal = 1
        self.fold_noise = fold_noise
        self.silent = silent
        if silent:
            self.verbose = False
            self.visualize = False

    def draw(self, idx=-1, max_display=None, format_str="{:.2f}"):
        '''
        assumes using ipython notebook
        idx: index to draw, <0 means using the summary abs mean plot
        '''
        viz_graph(self.credit2dot(format_str=format_str,
                                  idx=idx,
                                  max_display=max_display))

    def draw_asv(self, idx=-1, max_display=None, format_str="{:.2f}"):
        '''
        asv view only shows impact of source features
        assumes using ipython notebook
        idx: index to draw, <0 means using the summary abs mean plot
        '''
        edge_credit = defaultdict(lambda: defaultdict(int))
        target_node = [node for node in self.graph if node.is_target_node][0]
        
        # fold non source node
        for node1, d in self.edge_credit.items():
            for node2, val in d.items():
                if len(node1.args) > 0: continue

                if idx < 0 and len(val) == 1:
                    idx = 0
                if idx < 0:
                    edge_credit[node1][target_node] += np.mean(np.abs(val))
                else:
                    edge_credit[node1][target_node] += val[idx]
                
        G = self.credit2dot_pygraphviz(edge_credit, format_str,
                                       idx, max_display)
        viz_graph(G)
        
    def credit(self, node, val):
        if node is None or node.from_node is None:
            return

        self.edge_credit[node.from_node][node] += val
        if self.verbose:
            print(f"assign {val[0]} credits to {node.from_node}->{node}")
            if self.visualize:
                if not self.dot.has_edge(node.from_node, node):
                    self.dot.add_edge(node.from_node, node)
                dot_edge = self.dot.get_edge(node.from_node, node)
                dot_edge.attr['color'] = "blue"
                label = dot_edge.attr['label'] or '0'
                dot_edge.attr['label'] = f"{label}+{val[0]}"
                dot_edge.attr['fontcolor'] = "blue"
                dot_edge.attr['penwidth'] = self.penwidth_stress
                viz_graph(self.dot)
                dot_edge.attr['penwidth'] = self.penwidth_normal                
                dot_edge.attr['color'] = "black"
                dot_edge.attr['fontcolor'] = "black"                
                dot_edge.attr['label'] = eval(dot_edge.attr['label'])
                
        self.credit(node.from_node, val) # propagate upward

    def dfs(self, node, order):
        if node.is_target_node:
            self.credit(node, node.val - node.last_val)
            return

        children_order = np.random.permutation(node.children)
        for c in children_order:

            c.from_node = node
            c.last_val = c.val
            c.visible_arg_values[node] = node.val
            c.val = c.f(*[c.visible_arg_values[arg] for arg in c.args])

            if self.verbose:
                print(f'turn on edge {node}->{c}')                
                print(f'{c} changes from {c.last_val[0]} to {c.val[0]}')
                if self.visualize:
                    if not self.dot.has_edge(node, c):
                        self.dot.add_edge(node, c)
                    dot_edge = self.dot.get_edge(node, c)
                    dot_edge.attr['color'] = "orange"
                    dot_c = self.dot.get_node(c)
                    label = dot_c.attr['label']
                    dot_c.attr['label'] = f"{label.split(':')[0]}: {c.val[0]:.1f} ({c.baseline[0]:.1f}->{c.target[0]:.1f})"
                    dot_c_color = dot_c.attr['color']
                    dot_edge.attr['penwidth'] = self.penwidth_stress
                    dot_c.attr['penwidth'] = self.penwidth_stress
                    dot_c.attr['color'] = 'orange'
                    viz_graph(self.dot)
                    dot_edge.attr['penwidth'] = self.penwidth_normal
                    dot_c.attr['penwidth'] = self.penwidth_normal
                    dot_edge.attr['color'] = "black"
                    if c.val == c.baseline:
                        dot_c.attr['color'] = dot_c_color or "black"
                    elif c.val == c.target:
                        dot_c.attr['color'] = "green"
                
            self.dfs(c, order)

    def viz_graph_init(self, graph):
        '''
        initialize self.dot with the graph structure
        '''
        dot = AGraph(directed=True)
        for node in topo_sort(graph):
            if node not in dot:
                dot.add_node(node,
                             label=f"{node.name}: {node.val[0]:.1f} ({node.baseline[0]:.1f}->{node.target[0]:.1f})")
            for p in node.args:
                dot.add_edge(p, node)

        self.dot = dot
        viz_graph(self.dot)

    def reset(self):
        '''reset the graph and initialize the visualization'''
        self.graph.reset()
        if self.visualize:
            self.viz_graph_init(self.graph)
        
    def run(self):
        '''
        run shap flow algorithm to fairly allocate credit
        '''
        sources = get_source_nodes(self.graph)
        # random sample valid timelines
        if self.silent:
            run_range = range(self.nruns)            
        else:
            run_range = tqdm.trange(self.nruns, desc='sampling')
        
        for i in run_range:
            # make value back to baselines
            self.reset()
            
            order = list(np.random.permutation(sources))
            if self.verbose:
                print(f"\n----> using order {order}")
                print("baselines " +\
                      ", ".join(map(lambda node: f"{node}: {node.last_val[0]}",
                                    order)))
            # follow the order
            for node in order:
                node.last_val = node.val # update last val
                node.val = node.target # turn on the node
                node.from_node = None # turn off the source

                if self.verbose:
                    print(f"turn on edge from external source to {node}")
                    print(f"{node} changes from {node.last_val[0]} to {node.val[0]}")
                    if self.visualize:
                        if node not in self.dot:
                            self.dot.add_node(node)
                        dot_node = self.dot.get_node(node)
                        label = dot_node.attr['label']
                        dot_node.attr['label'] = f"{label.split(':')[0]}: {node.val[0]:.1f} ({node.baseline[0]:.1f}->{node.target[0]:.1f})"
                        dot_node.attr['penwidth'] = self.penwidth_stress
                        dot_node_color = dot_node.attr['color']
                        dot_node.attr['color'] = 'orange'
                        viz_graph(self.dot)
                        dot_node.attr['penwidth'] = self.penwidth_normal
                        if node.val == node.baseline:
                            dot_node.attr['color'] = dot_node_color or "black"
                        elif node.val == node.target:
                            dot_node.attr['color'] = "green"
                        
                self.dfs(node, order)

    def print_credit(self, edge_credit=None):
        if edge_credit is None: edge_credit = self.edge_credit
        for node1, d in edge_credit.items():
            for node2, val in d.items():
                print(f'credit {node1}->{node2}: {val/self.nruns}')


    def credit2dot_pygraphviz(self, edge_credit, format_str, idx=-1,
                              max_display=None):
        '''
        pygraphviz version of credit2dot
        idx: the index of target to visualize, if negative assumes sum
        '''
        G = AGraph(directed=True)

        edge_values = []
        max_v = 0
        for node1, d in edge_credit.items():
            for node2, val in d.items():
                max_v = max(abs(val/self.nruns), max_v)
                edge_values.append(abs(val/self.nruns))

        edge_values = sorted(edge_values)
        if max_display is None or max_display >= len(edge_values):
            min_v = 0
        else:
            min_v = edge_values[-max_display]
        
        for node1, d in edge_credit.items():
            for node2, val in d.items():
                
                v = val/self.nruns
                edge_label = format_str.format(v)
                if abs(v) < min_v: continue

                red = "#ff0051"
                blue = "#008bfb"
                if idx < 0:
                    color = f"{blue}ff"
                else:
                    color = f"{blue}ff" if v < 0 else f"{red}ff" # blue and red
                
                max_w = 5
                min_w = 0.05
                width = abs(v) / max_v * (max_w - min_w) + min_w
                
                if node1.is_dummy_node:
                    continue # should be covered in the next case

                if node2.is_dummy_node: # use the only direct child
                    node2 = node2.children[0]

                for node in [node1, node2]:
                    if node not in G:
                        if node.is_noise_node and self.fold_noise:
                            G.add_node(node, shape="point")
                        else:
                            shape = 'box' if node.is_categorical else 'ellipse'
                            if idx < 0:
                                G.add_node(node, label=node.name, shape=shape)
                            else:
                                txt = self.graph.display_translator\
                                    [node.name](node.target[idx])
                                if type(txt) is str:
                                    fmt = "{}: {}"
                                else:
                                    fmt = "{}: " + format_str
                                G.add_node(node, label=\
                                           fmt.format(node, txt),
                                           shape=shape)

                G.add_edge(node1, node2)
                e = G.get_edge(node1, node2)                
                e.attr["weight"] = v
                e.attr["penwidth"] = width
                e.attr["color"] = color
                e.attr["label"] = edge_label
                min_c, max_c = 60, 255
                alpha = "{:0>2}".format(hex(
                    int(abs(v) / max_v * (max_c - min_c) + min_c)
                )[2:]) # skip 0x
                if idx < 0:
                    e.attr["fontcolor"] = f"{blue}{alpha}"
                else:
                    e.attr["fontcolor"] = f"{blue}{alpha}" if v < 0 else\
                        f"{red}{alpha}"
        return G
        
    def credit2dot(self, format_str="{:.2f}", idx=-1, max_display=None):
        '''
        convert the graph to pydot graph for visualization
        e.g.:
        G = cf.credit2dot()
        viz_graph(G)

        idx: the index of target to visualize, if negative assumes sum
        max_display: max number of edges attribution to display
        '''
        edge_credit = defaultdict(lambda: defaultdict(int))
        
        # simplify for dummy intermediate node for multi-graph
        for node1, d in self.edge_credit.items():
            for node2, val in d.items():
                if node1.is_dummy_node:
                    continue # should be covered in the next case
                if node2.is_dummy_node:
                    node2 = node2.children[0]

                if idx < 0 and len(val) == 1:
                    idx = 0
                if idx < 0:
                    edge_credit[node1][node2] += np.mean(np.abs(val))
                else:
                    edge_credit[node1][node2] += val[idx]

        return self.credit2dot_pygraphviz(edge_credit, format_str,
                                          idx, max_display)

class ParallelCreditFlow:
    
    def __init__(self, graph, nruns=10, njobs=1, fold_noise=True):
        ''' 
        An embarassingly parallel implementaion of credit flow

        graph: causal graph to explain
        nruns: number of sampled valid timelines and permutations
        fold_noise: whether to show noise node as a point
        njobs: number of parallel jobs
        '''        
        njobs = min(nruns, njobs)
        self.cf = CreditFlow(graph,
                             nruns=nruns // njobs,
                             silent=False,
                             fold_noise=fold_noise)
        self.cf.reset() # to get baseline and target setup
        self.njobs = njobs
        self.nruns = nruns
        self.graph = graph
        print(f"{nruns} runs with {njobs} jobs")

        def wrap_run(cf):
            cf.run()

            edge_credit = {}
            for node1, d in cf.edge_credit.items():
                for node2, val in d.items():
                    if node1.name not in edge_credit:
                        edge_credit[node1.name] = {}
                    if node2.name not in edge_credit[node1.name]:
                        edge_credit[node1.name][node2.name] = 0
                    edge_credit[node1.name][node2.name] += val

            return edge_credit

        self.wrap_run = wrap_run

    def run_subprocess(self):
        '''
        run with subprocess
        problem: too slow

        1. serialize self.cf: s = dill.dumps(self.cf)
        2. pass s to an external file that does wrap_run
        3. serialize its output with dill and print to output
        4. use subprocess to capture the output and convert back to dict
        '''
        pwd = os.path.dirname(os.path.realpath(__file__))
        temp_dir = f"{pwd}/tmp"
        os.system(f'mkdir -p {temp_dir}')
        
        fns = [unique_filename(temp_dir) for _ in range(self.njobs + 1)]
        with open(fns[0], 'wb') as f:
            dill.dump(self.cf, f)

        procs = []
        commands = [["python", f"{pwd}/wrap_run.py", fns[0], fns[i+1]]\
                    for i in range(self.njobs)]
        for i, command in enumerate(commands):
            p = subprocess.Popen(command)
            procs.append(p)

        # join
        while True:
            job_status = [p.poll() == None for p in procs]
            if sum(job_status) > 0: # job active
                print(f"wait {job_status}...")
                time.sleep(1)
            else:
                break

        print("done")
        edge_credit_list = [dill.load(open(fns[i+1], "rb")) \
                            for i in range(self.njobs)]
        
        # combine edge credits
        name2node = dict((node.name, node) for node in self.cf.graph)
        for edge_credit in edge_credit_list:
            for node1_name, d in edge_credit.items():
                for node2_name, val in d.items():
                    self.cf.edge_credit[name2node[node1_name]]\
                        [name2node[node2_name]] +=  val / len(edge_credit_list)

        # clean up temp files
        subprocess.Popen(['rm', f"{temp_dir}/*"])

    def run_dill(self):
        '''
        multi-processing can avoid deadlock by using the "spawn" method,
        but, it doesn't pickle closures correctly. This method uses dill
        to do the pickling and then attempt to set "spawn" if possible
        problem: still not fast, similar speed as subprocess
        '''

        if mp.get_start_method(allow_none=True) != "spawn":
            warnings.warn("May deadlock with numpy! Plz set\
            multiprocessing: mp.set_start_method('spawn')")
            
        pool = mp.Pool(self.njobs)
        cf_str = dill.dumps(self.cf)
        edge_credit_list = pool.map(dill_run,
                                    [cf_str for _ in range(self.njobs)])

        # combine edge credits
        name2node = dict((node.name, node) for node in self.cf.graph)
        for edge_credit in edge_credit_list:
            for node1_name, d in edge_credit.items():
                for node2_name, val in d.items():
                    self.cf.edge_credit[name2node[node1_name]]\
                        [name2node[node2_name]] +=  val / len(edge_credit_list)

    def run_thread(self):
        '''
        use thread form joblib, but still prob b/c of GIL
        '''
        cfs = [CreditFlow(copy.deepcopy(self.graph),
                          nruns=self.nruns // self.njobs,
                          silent=True) for _ in range(self.njobs)]

        joblib.Parallel(n_jobs=self.njobs, prefer="threads")\
            (joblib.delayed(cf.run)()\
             for cf in cfs)

        # combine edge credits
        name2node = dict((node.name, node) for node in self.cf.graph)
        for cf in cfs:
            edge_credit = cf.edge_credit
            for node1, d in edge_credit.items():
                for node2, val in d.items():
                    self.cf.edge_credit[name2node[node1.name]]\
                        [name2node[node2.name]] +=  val / self.njobs

    def run_synthetic_process(self, method='mpd'):
        '''
        best for the random synthetic data
        
        problem: 
        - may deadlock due to numpy's lock
        - labmda not work with mp and joblib
        - mp_on_dill does not work with "spawn"

        deadlock can be solved by changing from "fork" to "spawn", but mpd
        does not work with "spawn"

        todo: investigate how to let it work with spawn

        method:
        'mpd': multiprocessing-on-dill
        'pathos': pathos

        Therefore this function cannot work with multi threading
        '''
        warnings.warn("May deadlock with numpy!")
        if method == 'mpd':
            import multiprocessing_on_dill as mpd
            pool = mpd.Pool(self.njobs)
        elif method == 'pathos':
            from pathos.pools import ProcessPool
            pool = ProcessPool(nodes=self.njobs)
            
        else:
            assert False, f"unkonwn method: {method}"
        
        # fill in edge_credit
        edge_credit_list = pool.map(self.wrap_run,
                                    [self.cf for _ in range(self.njobs)])
        

        # combine edge credits
        name2node = dict((node.name, node) for node in self.cf.graph)
        for edge_credit in edge_credit_list:
            for node1_name, d in edge_credit.items():
                for node2_name, val in d.items():
                    self.cf.edge_credit[name2node[node1_name]]\
                        [name2node[node2_name]] +=  val / len(edge_credit_list)

    def draw(self, *args, **kwargs):
        return self.cf.draw(*args, **kwargs)

    def draw_asv(self, *args, **kwargs):
        return self.cf.draw_asv(*args, **kwargs)

class GraphExplainer:

    def __init__(self, graph, X, nruns=100):
        '''
        graph: graph to explain
        X: background value samples from X, assumes dataframe
        nruns: how many runs for each data point
        '''
        assert type(X) == pd.DataFrame, \
            "assume data frame with column names matching node names"
        
        self.graph = copy.deepcopy(graph)
        self.nruns = nruns
        assert type(X) == pd.DataFrame, \
            "assume data frame with column names matching node names"
        self.bg = X        
        
    def _idx_f(self, idx, f):
        '''helper to save context'''
        def f_():
            return f(idx)
        return f_

    def set_noise_sampler(self):
        '''
        set baseline and target sampler for noise terms for self.graph
        we automatically add the noise node for each node if its computed
        target value differs from the actual value

        Assumptions:
        1. if the computation function output a (n, d) matrix: then we 
        assume the variable is discrete, otherwise we assume it is 
        continuous
        
        determine the following attribute for noise
        target: value that is consistent with the data
        baseline: if categorical just sample from 0-1, else sample from 
                  empirical distribution of the background
        '''
        def node_f_num(f):
            '''
            decorator for numerical node function to include noise
            
            f: original node function without noise
            treat noise as additive noise
            '''
            def f_(*args):
                noise = args[-1]
                return f(*args[:-1]) + noise
                
            return f_

        def node_f_cat(f):
            '''
            decorator for categorical node function to include noise
            
            f: original node function without noise
            treat noise as inverse sampling
            '''
            def f_(*args):
                noise = args[-1] # (n,)
                computed = f(*args[:-1]) # (n, d)
                assert (computed[:, -1].sum() > 0.99).all(),\
                    "need output of softmax"
                computed[:, -1] = 1.01 # avoid numerical issue
                return (computed.cumsum(1).T > noise).T.argmax(1)
                
            return f_
        
        bg_values = self.graph.sample_all(self.graph.baseline_sampler)
        fg_values = self.graph.sample_all(self.graph.target_sampler)
        
        for node in topo_sort(self.graph):

            if len(node.args) == 0: # source node
                node.bg_val = bg_values[node.name]
                node.fg_val = fg_values[node.name]                
                
            if len(node.args) > 0:
                
                bg_computed = node.f(*[arg.bg_val for arg in node.args])
                fg_computed = node.f(*[arg.fg_val for arg in node.args])
                
                if node.name in bg_values:
                    bg_sampled = bg_values[node.name]
                    fg_sampled = fg_values[node.name]
                    
                    node.bg_val = bg_sampled
                    node.fg_val = fg_sampled
                    
                    if bg_sampled.shape != bg_computed.shape or \
                       (bg_sampled != bg_computed).any():
                        # add a noise node
                        noise_node = Node(node.name + " noise",
                                          is_noise_node=True)
                        self.graph.add_node(noise_node)
                        
                        # change the function dependence of the node
                        node.add_arg(noise_node)
                        if bg_sampled.shape != bg_computed.shape:
                            node.is_categorical = True
                            # categorical variables
                            # wrapper for scope
                            def wrap_bg_sampler(len_fg):
                                def f_():
                                    return np.random.uniform(0, 1, len_fg)
                                return f_

                            def prob_range(cum, s):
                                '''
                                cum is (n, d) cumulative sum, 
                                s is (n,) selector
                                return lower and upper probability 
                                needed to sample
                                '''
                                upper = cum[np.arange(len(s)), s]
                                s = s - 1
                                zero_loc = s < 0
                                s[s < 0] = 0
                                lower = cum[np.arange(len(s)), s]
                                lower[zero_loc] = 0
                                return lower, upper
                            

                            def wrap_fg_sampler(lower_prob, upper_prob):
                                ''' 
                                lower_prob, upper_prob: output of prob_range
                                '''
                                def f_():
                                    return np.random.uniform(lower_prob,
                                                             upper_prob)
                                return f_
                            
                            # reset node.f
                            node.f = node_f_cat(node.f)
                            # add baseline and target sampler for noise_node
                            self.graph.baseline_sampler[noise_node.name] = \
                                wrap_bg_sampler(len(self.fg))

                            lower, upper = prob_range(fg_computed.cumsum(1),
                                                      fg_sampled.astype(int))
                            self.graph.target_sampler[noise_node.name] = \
                                wrap_fg_sampler(lower, upper)
                            
                        else: # numerical variables

                            # wrapper for scope
                            def wrap_bg_sampler(bg_diff, len_fg):
                                def f_():
                                    return bg_diff[np.random.choice(len(bg_diff),
                                                                    len_fg)]
                                return f_

                            def wrap_fg_sampler(fg_diff):
                                def f_():
                                    return fg_diff
                                return f_
                            
                            # reset node.f
                            node.f = node_f_num(node.f)
                            # add baseline and target sampler for noise_node
                            bg_diff = bg_sampled - bg_computed
                            fg_diff = fg_sampled - fg_computed
                            self.graph.baseline_sampler[noise_node.name] = \
                                wrap_bg_sampler(bg_diff, len(self.fg))
                            self.graph.target_sampler[noise_node.name] = \
                                wrap_fg_sampler(fg_diff)
                else:
                    node.bg_val = bg_computed
                    node.fg_val = fg_computed
            

    def prepare_graph(self, X):
        '''
        X : pandas.DataFrame
            A matrix of samples (# samples x # features) on which to explain 
            the model's output.
        '''
        assert type(X) == pd.DataFrame, \
            "assume data frame with column names matching node names"
        assert (self.bg.columns == X.columns).all(), "feature names must match"
        self.fg = X
        names = X.columns
        bg = np.array(self.bg)
        rc = np.random.choice
        self.graph.baseline_sampler = dict((name,
                                            self._idx_f(i, lambda i: \
                                                        bg[rc(len(bg),
                                                              len(X))][:, i]))
                                           for i, name in enumerate(names))
        
        self.graph.target_sampler = dict((name, self._idx_f(i, lambda i: \
                                                            np.array(X)[:, i]))
                                         for i, name in enumerate(names))
        self.set_noise_sampler()
        
    def shap_values(self, X):
        """ Estimate the SHAP values for a set of samples.

        Parameters
        ----------
        X : pandas.DataFrame
            A matrix of samples (# samples x # features) on which to explain 
            the model's output.

        Returns
        -------
        a credit flow object
        """
        self.prepare_graph(X)
        cf = CreditFlow(self.graph, nruns=self.nruns)
        cf.run()
        return cf

##### helper functions
# graph visualization
def dill_run(cf_str):
    cf = dill.loads(cf_str)
    cf.run()

    edge_credit = {}
    for node1, d in cf.edge_credit.items():
        for node2, val in d.items():
            if node1.name not in edge_credit:
                edge_credit[node1.name] = {}
            if node2.name not in edge_credit[node1.name]:
                edge_credit[node1.name][node2.name] = 0
            edge_credit[node1.name][node2.name] += val

    return edge_credit
    
def viz_graph(G):
    '''only applicable in ipython notebook setting 
    convert G (pygraphviz) to graphviz format and display with 
    ipython display
    '''
    display(Source(G.string()))

def save_graph(G, name, layout="dot"):
    '''
    G is a pygraphviz object;
    save G to a file with name
    '''
    G.layout(prog=layout)
    G.draw(name)
    
def translator(names, X, X_display):
    '''
    X and X_display are assumed to be convertible to np array
    of shape (n, d)
    '''
    def f(A, B):
        d = dict((a, b) for a, b in zip(A, B))
            
        def f_(val):
            if val in d:
                return d[val]
            return val
        return f_
    
    res = {}
    X = np.array(X).T
    X_display = np.array(X_display).T
    assert X.shape == X_display.shape, "translation must have same shape"
    
    for i, name in enumerate(names):
        res[name] = f(X[i], X_display[i])
    return res

# graph algorithm
def get_source_nodes(graph):
    indegrees = dict((node, len(node.args)) for node in graph)
    sources = [node for node in graph if indegrees[node] == 0]
    return sources

def topo_sort(graph):
    '''
    topological sort of a graph
    '''
    order = []
    indegrees = dict((node, len(node.args)) for node in graph)
    sources = [node for node in graph if indegrees[node] == 0]
    while sources:
        sources = list(np.random.permutation(sources))
        s = sources.pop()
        order.append(s)
        for u in np.random.permutation(s.children):
            indegrees[u] -= 1 # s is satisfied
            if indegrees[u] == 0:
                sources.append(u)
    return order

# graph operation    
def flatten_graph(graph):
    '''
    given a graph, return a graph with the graph flattened
    '''

    def node_function(f, node):
        '''
        helper function to record node in context
        f: function Node -> val
        '''
        def f_():
            return f(node)
        return f_

    graph = copy.deepcopy(graph)
    for node in topo_sort(graph):

        if not node.is_target_node:
            if node.args != []:
                
                for arg in node.args: # remove parent's link to this node
                    idx = arg.children.index(node)
                    arg.children = arg.children[:idx] + arg.children[idx+1:]
                
                node.spare_args = node.args
                node.args = [] # remove this node's link to parent
                
                graph.baseline_sampler[node.name] = node_function(lambda node:\
                                                                  node.f(*[graph.sample(graph.baseline_sampler, arg.name)\
                                                                           for arg in node.spare_args]),
                                                                  node)
                graph.target_sampler[node.name] = node_function(lambda node:\
                                                                node.f(*[graph.sample(graph.target_sampler, arg.name)\
                                                                         for arg in node.spare_args]),
                                                                node)
                
    return graph

def eval_graph(graph, val_dict):
    for node in topo_sort(graph):
        if node.name in val_dict:
            v = val_dict[node.name]
            if not isinstance(v, Iterable):
                v = np.array([v])
            node.val = v
        else:
            node.val = node.f(*[a.val for a in node.args])

        if node.is_target_node:
            return node.val

def merge_nodes(nodes):
    '''
    nodes: assume nodes follow topological order, with the last being the target
    helper function for boundary_graph
    '''
    def merge_h(node1, node2):
        '''assume node 1 depend on node2, otherwise return node1
        helper function for merge_nodes'''
        if node2 in node1.args:

            def create_f(node1, node2):
                def f(*args):
                    node2_args = args[:len(node2.args)]
                    node1_args = args[len(node2.args):]
                    v = node2.f(*node2_args)
                    args = []
                    idx = 0
                    for a in node1.args:
                        if a.name == node2.name:
                            args.append(v)
                        else:
                            args.append(node1_args[idx])
                            idx += 1
                    return node1.f(*args)
                return f

            args = node2.args + [a for a in node1.args if a.name != node2.name]
            node1 = Node(node1.name, create_f(node1, node2), args)

        return node1


    y = nodes[-1]
    assert y.is_target_node, "assumes last one is the target"
    for node in nodes[:-1][::-1]:
        y = merge_h(y, node)
        y.is_target_node = True
    
    # rewire parents: add dumminess to work with multi graph
    dummy_nodes = [Node(arg.name + '_dummy{}'.format(i), lambda x: x, [arg],
                        is_dummy_node=True) for i, arg in enumerate(y.args)]
    y.args = dummy_nodes
    
    for node in dummy_nodes:
        node.children = [y]
            
    # remove link to old node
    for node in dummy_nodes:
        for parent in node.args:
            for n in nodes:
                parent.children = [c for c in parent.children if c.name != n.name]
    
    return y, dummy_nodes
        
def boundary_graph(graph, boundary_nodes=[]):
    '''
    boundary nodes are names of nodes on the boundary
    we collapse all model nodes into a single node
    default just use the source nodes
    '''
    graph = copy.deepcopy(graph)
    
    # find the boundary node's ancestor and include it in the graph with dfs
    boundary_nodes = [node for node in graph if node.name in boundary_nodes] \
        + get_source_nodes(graph)
    visited = set()
    def dfs(node):
        if node in visited: return
        visited.add(node)
        for n in node.args:
            dfs(n)
    
    for node in boundary_nodes:
        dfs(node)
    boundary_nodes = visited
    
    # group C = (D, M)
    nodes_in_d = [] # data side
    nodes_to_merge = []
    for node in topo_sort(graph):
        if node not in boundary_nodes:
            nodes_to_merge.append(node)
            if node.is_target_node:
                break
        else:
            nodes_in_d.append(node)
    
    # merge node in M
    node, dummy_nodes = merge_nodes(nodes_to_merge)
    nodes = nodes_in_d + [node] + dummy_nodes

    return Graph(nodes, graph.baseline_sampler, graph.target_sampler)

def single_source_graph(graph):
    '''
    check if a graph has a single source. If not,
    add an artificial source node
    collapse source noise node that are noise by setting them to be dummy
    '''
    graph = copy.deepcopy(graph)
    sources = get_source_nodes(graph)
    if len(sources) == 1:
        return graph
    
    s = Node('seed', is_noise_node=True)
    for node in sources:
        node.add_arg(s)
        if node.is_noise_node:
            node.is_dummy_node = True

        def create_f(graph, node):
            def f(s):
                if s == 0:
                    return graph.sample(graph.baseline_sampler, node.name)
                else:
                    return graph.sample(graph.target_sampler, node.name)
                
            return f
        
        node.f = create_f(graph, node)

    graph.baseline_sampler[s.name] = lambda: 0
    graph.target_sampler[s.name] = lambda: 1
    graph.add_node(s)
    graph.reset()
    return graph

def hcluster_graph(graph, source_names, cluster_matrix, verbose=False):
    '''
    cluster_matrix: of form https://docs.scipy.org/doc/scipy/reference/generated/scipy.cluster.hierarchy.linkage.html#scipy.cluster.hierarchy.linkage
    source_names: name of the source nodes
    graph: a flat graph with input features
    '''
    graph = copy.deepcopy(graph)
    nodes = sorted(get_source_nodes(graph),
                   key=lambda node: source_names.index(node.name))
    for row in cluster_matrix:
        node1 = nodes[int(row[0])]
        node2 = nodes[int(row[1])]
        if verbose:
            print(f'merging {node1} and {node2} (dist={row[2]}, n={row[3]})')
        s = Node(f"{node1} x {node2}", is_noise_node=True)
        node1.add_arg(s)
        node2.add_arg(s)
        
        def create_f(graph, node):
            def f(s):
                if s == 0:
                    return graph.sample(graph.baseline_sampler, node.name)
                else:
                    return graph.sample(graph.target_sampler, node.name)
            return f

        node1.f = create_f(graph, node1)
        node2.f = create_f(graph, node2)

        graph.baseline_sampler[s.name] = lambda: 0
        graph.target_sampler[s.name] = lambda: 1
        graph.add_node(s)
        nodes.append(s)
        
    graph.reset()
    return graph

# sample graph
def build_graph():
    '''
    build and return a graph (list of nodes), to be runnable in main
    '''
    # build the graph: x1->x2, y = x1 + x2
    x1 = Node('x1')
    x2 = Node('x2', lambda x1: x1, [x1])
    y  = Node('target', lambda x1, x2: x1 + x2, [x1, x2], is_target_node=True)
    
    # initialize the values from data: now is just specified
    graph = Graph([x1, x2, y],
                  # sample baseline
                  {'x1': lambda: 0},
                  # target to explain
                  {'x1': lambda: 1})
    
    return graph

# other utility functions
def unique_filename(dir):
    return os.path.join(dir, str(uuid.uuid4()))

# sample runs
def example_detailed():
    ''' 
    an example with detailed control over the algorithm
    '''
    graph = build_graph()    
    cf = CreditFlow(graph, verbose=False, nruns=1)
    cf.run()
    cf.print_credit() # cf.draw() if using ipython notebook

def example_concise():
    '''
    an example with recommended way of running the module
    '''
    graph = build_graph()
    explainer = GraphExplainer(graph, pd.DataFrame.from_dict({'x1': [0]}))
    cf = explainer.shap_values(pd.DataFrame.from_dict({'x1': [1]}))
    cf.print_credit() # cf.draw() if using ipython notebook
    
if __name__ == '__main__':
    example_concise()
