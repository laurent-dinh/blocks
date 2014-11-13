import argparse
import logging
import inspect
import pprint

import numpy
import theano
try:
    from groundhog.mainLoop import MainLoop
    from groundhog.trainer.SGD import SGD
    from matplotlib import pyplot
except:
    pass  # TODO matplotlib as dependency?
from theano import tensor

from blocks.bricks import Brick, GatedRecurrent, Identity, Tanh, MLP
from blocks.select import Selector
from blocks.graph import Cost
from blocks.sequence_generators import SequenceGenerator, SimpleReadout
from blocks.initialization import Orthogonal, IsotropicGaussian, Constant
from blocks.groundhog import GroundhogIterator, GroundhogState, GroundhogModel
from blocks.serialization import load_params

floatX = theano.config.floatX
logger = logging.getLogger()


class Readout(SimpleReadout):
    """Specifies input names, dimensionality and cost for SimpleReadout."""

    def __init__(self):
        super(Readout, self).__init__(readout_dim=1,
                                      source_names=['states'])

    @Brick.apply_method
    def cost(self, readouts, outputs):
        """Compute MSE."""
        return ((readouts - outputs) ** 2).sum(axis=readouts.ndim - 1)


class AddParameters(Brick):
    """Adds dependency on parameters to a transition function.

    In fact an improved version of this brick should be moved
    to the main body of the library, because it is clearly reusable
    (e.g. it can be a part of Encoder-Decoder translation model.

    """

    @Brick.lazy_method
    def __init__(self, transition, num_params, params_name,
                 weights_init, biases_init, **kwargs):
        super(AddParameters, self).__init__(**kwargs)
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs

        signature = self.transition.apply.signature()
        self.input_names = signature.forkable_input_names
        self.state_name = signature.state_names[0]
        assert len(signature.state_names) == 1

        self.adders = [MLP([Identity()], name="add_{}".format(input_name))
                       for input_name in self.input_names]
        # Could be also several init bricks, one for each of the states
        self.init = MLP([Identity()], name="init")
        self.children = [self.transition] + self.adders + [self.init]

    def _push_allocation_config(self):
        signature = self.transition.apply.signature()
        for adder, input_name in zip(self.adders, self.input_names):
            adder.dims[0] = self.num_params
            adder.dims[-1] = signature.dims[input_name]
        self.init.dims[0] = self.num_params
        self.init.dims[-1] = signature.dims[signature.state_names[0]]
        assert len(signature.state_names) == 1

    def _push_initialization_config(self):
        for child in self.children:
            if self.weights_init:
                child.weights_init = self.weights_init
            if self.biases_init:
                child.biases_init = self.biases_init

    @Brick.apply_method
    def apply(self, **kwargs):
        inputs = {name: kwargs.pop(name) for name in self.input_names}
        params = kwargs.pop("params")
        for name, adder in zip(self.input_names, self.adders):
            inputs[name] = inputs[name] + adder.apply(params)
        kwargs.update(inputs)
        return self.transition.apply(**kwargs)

    @apply.signature_method
    def apply_signature(self, **kwargs):
        signature = self.transition.apply.signature()
        signature.context_names.append(self.params_name)
        signature.state_init_funcs[self.state_name] = self.initialize_state
        signature.dims[self.params_name] = self.num_params
        return signature

    @Brick.apply_method
    def initialize_state(self, *args, **kwargs):
        return self.init.apply(kwargs[self.params_name])


class SeriesIterator(GroundhogIterator):
    """Training data generator."""

    def __init__(self, rng, func, seq_len, batch_size):
        self.__dict__.update(**locals())
        del self.self
        self.num_params = len(inspect.getargspec(self.func).args) - 1

    def next(self):
        """Generate random sequences from the family."""
        params = self.rng.uniform(size=(self.batch_size, self.num_params))
        params = params.astype(floatX)
        x = numpy.zeros((self.seq_len, self.batch_size, 1), dtype=floatX)
        for i in range(self.seq_len):
            x[i, :, 0] = self.func(*([list(params.T)] +
                                     [i * numpy.ones(self.batch_size)]))

        return dict(x=x, params=params)


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s: %(name)s: %(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        "Case study of generating simple 1d sequences with RNN.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "mode", choices=["train", "plot"],
        help="The mode to run. Use `train` to train a new model"
             " and `plot` to plot a sequence generated by an"
             " existing one.")
    parser.add_argument(
        "prefix", default="sine",
        help="The prefix for model, timing and state files")
    parser.add_argument(
        "--input-noise", type=float, default=0.0,
        help="Adds Gaussian noise of given intensity to the "
             " training sequences.")
    parser.add_argument(
        "--function", default="lambda a, x: numpy.sin(a * x)",
        help="An analytical description of the sequence family to learn."
             " The arguments before the last one are considered parameters.")
    parser.add_argument(
        "--steps", type=int, default=100,
        help="Number of steps to plot")
    parser.add_argument(
        "--params",
        help="Parameter values for plotting")
    args = parser.parse_args()

    function = eval(args.function)
    num_params = len(inspect.getargspec(function).args) - 1

    transition = GatedRecurrent(
        name="transition", activation=Tanh(), dim=10,
        weights_init=Orthogonal())
    with_params = AddParameters(transition, num_params, "params",
                                name="with_params")
    generator = SequenceGenerator(
        Readout(), with_params,
        weights_init=IsotropicGaussian(0.01), biases_init=Constant(0),
        name="generator")
    generator.allocate()
    logger.debug("Parameters:\n" +
                 pprint.pformat(Selector(generator).get_params().keys()))

    if args.mode == "train":
        seed = 1
        rng = numpy.random.RandomState(seed)
        batch_size = 10

        generator.initialize()

        cost = Cost(generator.cost(tensor.tensor3('x'),
                                   params=tensor.matrix("params")).sum())
        cost.apply_noise(cost.inputs, args.input_noise)

        gh_model = GroundhogModel(generator, cost)
        state = GroundhogState(args.prefix, batch_size,
                               learning_rate=0.0001).as_dict()
        data = SeriesIterator(rng, function, 100, batch_size)
        trainer = SGD(gh_model, state, data)
        main_loop = MainLoop(data, None, None, gh_model, trainer, state, None)
        main_loop.main()
    elif args.mode == "plot":
        load_params(generator,  args.prefix + "model.npz")

        params = tensor.matrix("params")
        sample = theano.function([params], generator.generate(
            params=params, n_steps=args.steps, batch_size=1, iterate=True))

        param_values = numpy.array(map(float, args.params.split()),
                                   dtype=floatX)
        states, outputs, costs = sample(param_values[None, :])
        actual = outputs[:, 0, 0]

        desired = numpy.array([function(*(list(param_values) + [T]))
                               for T in range(args.steps)])
        print "MSE: {}".format(((actual - desired) ** 2).sum())

        pyplot.plot(numpy.hstack([actual[:, None], desired[:, None]]))
        pyplot.show()
    else:
        assert False


if __name__ == "__main__":
    main()