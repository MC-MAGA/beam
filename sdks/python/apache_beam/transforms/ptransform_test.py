#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Unit tests for the PTransform and descendants."""

# pytype: skip-file

import collections
import operator
import os
import pickle
import random
import re
import typing
import unittest
from functools import reduce
from typing import Optional
from unittest.mock import patch

import hamcrest as hc
import numpy as np
import pytest
from parameterized import parameterized_class

import apache_beam as beam
import apache_beam.transforms.combiners as combine
from apache_beam import pvalue
from apache_beam import typehints
from apache_beam.io.iobase import Read
from apache_beam.metrics import Metrics
from apache_beam.metrics.metric import MetricsFilter
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import TypeOptions
from apache_beam.portability import common_urns
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.test_stream import TestStream
from apache_beam.testing.util import SortLists
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import equal_to
from apache_beam.transforms import WindowInto
from apache_beam.transforms import trigger
from apache_beam.transforms import window
from apache_beam.transforms.display import DisplayData
from apache_beam.transforms.display import DisplayDataItem
from apache_beam.transforms.ptransform import PTransform
from apache_beam.transforms.window import TimestampedValue
from apache_beam.typehints import with_input_types
from apache_beam.typehints import with_output_types
from apache_beam.typehints.typehints_test import TypeHintTestCase
from apache_beam.utils.timestamp import Timestamp
from apache_beam.utils.windowed_value import WindowedValue

# Disable frequent lint warning due to pipe operator for chaining transforms.
# pylint: disable=expression-not-assigned


class PTransformTest(unittest.TestCase):
  def assertStartswith(self, msg, prefix):
    self.assertTrue(
        msg.startswith(prefix), '"%s" does not start with "%s"' % (msg, prefix))

  def test_str(self):
    self.assertEqual(
        '<PTransform(PTransform) label=[PTransform]>', str(PTransform()))

    pa = TestPipeline()
    res = pa | 'ALabel' >> beam.Impulse()
    self.assertEqual('AppliedPTransform(ALabel, Impulse)', str(res.producer))

    pc = TestPipeline()
    res = pc | beam.Impulse()
    inputs_tr = res.producer.transform
    inputs_tr.inputs = ('ci', )
    self.assertEqual(
        "<Impulse(PTransform) label=[Impulse] inputs=('ci',)>", str(inputs_tr))

    pd = TestPipeline()
    res = pd | beam.Impulse()
    side_tr = res.producer.transform
    side_tr.side_inputs = (4, )
    self.assertEqual(
        '<Impulse(PTransform) label=[Impulse] side_inputs=(4,)>', str(side_tr))

    inputs_tr.side_inputs = ('cs', )
    self.assertEqual(
        """<Impulse(PTransform) label=[Impulse] """
        """inputs=('ci',) side_inputs=('cs',)>""",
        str(inputs_tr))

  def test_named_annotations(self):
    t = beam.Impulse()
    t.annotations = lambda: {'test': 'value'}
    named_t = 'Name' >> t
    self.assertEqual(named_t.annotations(), {'test': 'value'})
    original_annotations = named_t.annotations()
    named_t.annotations = lambda: {'another': 'value', **original_annotations}
    # Verify this is reflected on the original transform,
    # which is what gets used in apply.
    self.assertEqual(t.annotations(), {'test': 'value', 'another': 'value'})

  def test_do_with_do_fn(self):
    class AddNDoFn(beam.DoFn):
      def process(self, element, addon):
        return [element + addon]

    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      result = pcoll | 'Do' >> beam.ParDo(AddNDoFn(), 10)
      assert_that(result, equal_to([11, 12, 13]))

  def test_do_with_unconstructed_do_fn(self):
    class MyDoFn(beam.DoFn):
      def process(self):
        pass

    with self.assertRaises(ValueError):
      with TestPipeline() as pipeline:
        pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
        pcoll | 'Do' >> beam.ParDo(MyDoFn)  # Note the lack of ()'s

  def test_do_with_callable(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      result = pcoll | 'Do' >> beam.FlatMap(lambda x, addon: [x + addon], 10)
      assert_that(result, equal_to([11, 12, 13]))

  def test_do_with_side_input_as_arg(self):
    with TestPipeline() as pipeline:
      side = pipeline | 'Side' >> beam.Create([10])
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      result = pcoll | 'Do' >> beam.FlatMap(
          lambda x, addon: [x + addon], pvalue.AsSingleton(side))
      assert_that(result, equal_to([11, 12, 13]))

  def test_do_with_side_input_as_keyword_arg(self):
    with TestPipeline() as pipeline:
      side = pipeline | 'Side' >> beam.Create([10])
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      result = pcoll | 'Do' >> beam.FlatMap(
          lambda x, addon: [x + addon], addon=pvalue.AsSingleton(side))
      assert_that(result, equal_to([11, 12, 13]))

  def test_do_with_do_fn_returning_string_raises_warning(self):
    ex_details = r'.*Returning a str from a ParDo or FlatMap is discouraged.'

    with self.assertRaisesRegex(Exception, ex_details):
      with TestPipeline() as pipeline:
        pipeline._options.view_as(TypeOptions).runtime_type_check = True
        pcoll = pipeline | 'Start' >> beam.Create(['2', '9', '3'])
        pcoll | 'Do' >> beam.FlatMap(lambda x: x + '1')

        # Since the DoFn directly returns a string we should get an
        # error warning us when the pipeliene runs.

  def test_do_with_do_fn_returning_dict_raises_warning(self):
    ex_details = r'.*Returning a dict from a ParDo or FlatMap is discouraged.'

    with self.assertRaisesRegex(Exception, ex_details):
      with TestPipeline() as pipeline:
        pipeline._options.view_as(TypeOptions).runtime_type_check = True
        pcoll = pipeline | 'Start' >> beam.Create(['2', '9', '3'])
        pcoll | 'Do' >> beam.FlatMap(lambda x: {x: '1'})

        # Since the DoFn directly returns a dict we should get an error warning
        # us when the pipeliene runs.

  def test_do_with_multiple_outputs_maintains_unique_name(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      r1 = pcoll | 'A' >> beam.FlatMap(lambda x: [x + 1]).with_outputs(main='m')
      r2 = pcoll | 'B' >> beam.FlatMap(lambda x: [x + 2]).with_outputs(main='m')
      assert_that(r1.m, equal_to([2, 3, 4]), label='r1')
      assert_that(r2.m, equal_to([3, 4, 5]), label='r2')

  @pytest.mark.it_validatesrunner
  def test_impulse(self):
    with TestPipeline() as pipeline:
      result = pipeline | beam.Impulse() | beam.Map(lambda _: 0)
      assert_that(result, equal_to([0]))

  # TODO(BEAM-3544): Disable this test in streaming temporarily.
  # Remove sickbay-streaming tag after it's resolved.
  @pytest.mark.no_sickbay_streaming
  @pytest.mark.it_validatesrunner
  def test_read_metrics(self):
    from apache_beam.io.utils import CountingSource

    class CounterDoFn(beam.DoFn):
      def __init__(self):
        # This counter is unused.
        self.received_records = Metrics.counter(
            self.__class__, 'receivedRecords')

      def process(self, element):
        self.received_records.inc()

    pipeline = TestPipeline()
    (pipeline | Read(CountingSource(100)) | beam.ParDo(CounterDoFn()))
    res = pipeline.run()
    res.wait_until_finish()
    # This counter is defined in utils.CountingSource.
    metric_results = res.metrics().query(
        MetricsFilter().with_name('recordsRead'))
    outputs_counter = metric_results['counters'][0]
    msg = outputs_counter.key.step
    cont = 'SDFBoundedSourceReader'
    self.assertTrue(cont in msg, '"%s" does not contain "%s"' % (msg, cont))
    self.assertEqual(outputs_counter.key.metric.name, 'recordsRead')
    self.assertEqual(outputs_counter.committed, 100)

  @pytest.mark.it_validatesrunner
  def test_par_do_with_multiple_outputs_and_using_yield(self):
    class SomeDoFn(beam.DoFn):
      """A custom DoFn using yield."""
      def process(self, element):
        yield element
        if element % 2 == 0:
          yield pvalue.TaggedOutput('even', element)
        else:
          yield pvalue.TaggedOutput('odd', element)

    with TestPipeline() as pipeline:
      nums = pipeline | 'Some Numbers' >> beam.Create([1, 2, 3, 4])
      results = nums | 'ClassifyNumbers' >> beam.ParDo(SomeDoFn()).with_outputs(
          'odd', 'even', main='main')
      assert_that(results.main, equal_to([1, 2, 3, 4]))
      assert_that(results.odd, equal_to([1, 3]), label='assert:odd')
      assert_that(results.even, equal_to([2, 4]), label='assert:even')

  @pytest.mark.it_validatesrunner
  def test_par_do_with_multiple_outputs_and_using_return(self):
    def some_fn(v):
      if v % 2 == 0:
        return [v, pvalue.TaggedOutput('even', v)]
      return [v, pvalue.TaggedOutput('odd', v)]

    with TestPipeline() as pipeline:
      nums = pipeline | 'Some Numbers' >> beam.Create([1, 2, 3, 4])
      results = nums | 'ClassifyNumbers' >> beam.FlatMap(some_fn).with_outputs(
          'odd', 'even', main='main')
      assert_that(results.main, equal_to([1, 2, 3, 4]))
      assert_that(results.odd, equal_to([1, 3]), label='assert:odd')
      assert_that(results.even, equal_to([2, 4]), label='assert:even')

  @pytest.mark.it_validatesrunner
  def test_undeclared_outputs(self):
    with TestPipeline() as pipeline:
      nums = pipeline | 'Some Numbers' >> beam.Create([1, 2, 3, 4])
      results = nums | 'ClassifyNumbers' >> beam.FlatMap(
          lambda x: [
              x, pvalue.TaggedOutput('even' if x % 2 == 0 else 'odd', x), pvalue
              .TaggedOutput('extra', x)
          ]).with_outputs()
      assert_that(results[None], equal_to([1, 2, 3, 4]))
      assert_that(results.odd, equal_to([1, 3]), label='assert:odd')
      assert_that(results.even, equal_to([2, 4]), label='assert:even')

  @pytest.mark.it_validatesrunner
  def test_multiple_empty_outputs(self):
    with TestPipeline() as pipeline:
      nums = pipeline | 'Some Numbers' >> beam.Create([1, 3, 5])
      results = nums | 'ClassifyNumbers' >> beam.FlatMap(
          lambda x:
          [x, pvalue.TaggedOutput('even'
                                  if x % 2 == 0 else 'odd', x)]).with_outputs()
      assert_that(results[None], equal_to([1, 3, 5]))
      assert_that(results.odd, equal_to([1, 3, 5]), label='assert:odd')
      assert_that(results.even, equal_to([]), label='assert:even')

  def test_do_requires_do_fn_returning_iterable(self):
    # This function is incorrect because it returns an object that isn't an
    # iterable.
    def incorrect_par_do_fn(x):
      return x + 5

    ex_details = r'.*FlatMap and ParDo must return an iterable.'

    with self.assertRaisesRegex(Exception, ex_details):
      with TestPipeline() as pipeline:
        pipeline._options.view_as(TypeOptions).runtime_type_check = True
        pcoll = pipeline | 'Start' >> beam.Create([2, 9, 3])
        pcoll | 'Do' >> beam.FlatMap(incorrect_par_do_fn)
        # It's a requirement that all user-defined functions to a ParDo return
        # an iterable.

  def test_do_fn_with_finish(self):
    class MyDoFn(beam.DoFn):
      def process(self, element):
        pass

      def finish_bundle(self):
        yield WindowedValue('finish', -1, [window.GlobalWindow()])

    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      result = pcoll | 'Do' >> beam.ParDo(MyDoFn())

      # May have many bundles, but each has a start and finish.
      def matcher():
        def match(actual):
          equal_to(['finish'])(list(set(actual)))
          equal_to([1])([actual.count('finish')])

        return match

      assert_that(result, matcher())

  def test_do_fn_with_windowing_in_finish_bundle(self):
    windowfn = window.FixedWindows(2)

    class MyDoFn(beam.DoFn):
      def process(self, element):
        yield TimestampedValue('process' + str(element), 5)

      def finish_bundle(self):
        yield WindowedValue('finish', 1, [windowfn])

    with TestPipeline() as pipeline:
      result = (
          pipeline
          | 'Start' >> beam.Create([1])
          | beam.ParDo(MyDoFn())
          | WindowInto(windowfn)
          | 'create tuple' >> beam.Map(
              lambda v, t=beam.DoFn.TimestampParam, w=beam.DoFn.WindowParam:
              (v, t, w.start, w.end)))
      expected_process = [
          ('process1', Timestamp(5), Timestamp(4), Timestamp(6))
      ]
      expected_finish = [('finish', Timestamp(1), Timestamp(0), Timestamp(2))]

      assert_that(result, equal_to(expected_process + expected_finish))

  def test_do_fn_with_start(self):
    class MyDoFn(beam.DoFn):
      def __init__(self):
        self.state = 'init'

      def start_bundle(self):
        self.state = 'started'

      def process(self, element):
        if self.state == 'started':
          yield 'started'
        self.state = 'process'

    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3])
      result = pcoll | 'Do' >> beam.ParDo(MyDoFn())

      # May have many bundles, but each has a start and finish.
      def matcher():
        def match(actual):
          equal_to(['started'])(list(set(actual)))
          equal_to([1])([actual.count('started')])

        return match

      assert_that(result, matcher())

  def test_do_fn_with_start_error(self):
    class MyDoFn(beam.DoFn):
      def start_bundle(self):
        return [1]

      def process(self, element):
        pass

    with self.assertRaises(RuntimeError):
      with TestPipeline() as p:
        p | 'Start' >> beam.Create([1, 2, 3]) | 'Do' >> beam.ParDo(MyDoFn())

  def test_map_builtin(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([[1, 2], [1], [1, 2, 3]])
      result = pcoll | beam.Map(len)
      assert_that(result, equal_to([1, 2, 3]))

  def test_flatmap_builtin(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([
          [np.array([1, 2, 3])] * 3, [np.array([5, 4, 3]), np.array([5, 6, 7])]
      ])
      result = pcoll | beam.FlatMap(sum)
      assert_that(result, equal_to([3, 6, 9, 10, 10, 10]))

  def test_filter_builtin(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([[], [2], [], [4]])
      result = pcoll | 'Filter' >> beam.Filter(len)
      assert_that(result, equal_to([[2], [4]]))

  def test_filter(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([1, 2, 3, 4])
      result = pcoll | 'Filter' >> beam.Filter(lambda x: x % 2 == 0)
      assert_that(result, equal_to([2, 4]))

  class _MeanCombineFn(beam.CombineFn):
    def create_accumulator(self):
      return (0, 0)

    def add_input(self, sum_count, element):
      (sum_, count) = sum_count
      return sum_ + element, count + 1

    def merge_accumulators(self, accumulators):
      sums, counts = zip(*accumulators)
      return sum(sums), sum(counts)

    def extract_output(self, sum_count):
      (sum_, count) = sum_count
      if not count:
        return float('nan')
      return sum_ / float(count)

  def test_combine_with_combine_fn(self):
    vals = [1, 2, 3, 4, 5, 6, 7]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(vals)
      result = pcoll | 'Mean' >> beam.CombineGlobally(self._MeanCombineFn())
      assert_that(result, equal_to([sum(vals) // len(vals)]))

  def test_combine_with_callable(self):
    vals = [1, 2, 3, 4, 5, 6, 7]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(vals)
      result = pcoll | beam.CombineGlobally(sum)
      assert_that(result, equal_to([sum(vals)]))

  def test_combine_with_side_input_as_arg(self):
    values = [1, 2, 3, 4, 5, 6, 7]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(values)
      divisor = pipeline | 'Divisor' >> beam.Create([2])
      result = pcoll | 'Max' >> beam.CombineGlobally(
          # Multiples of divisor only.
          lambda vals, d: max(v for v in vals if v % d == 0),
          pvalue.AsSingleton(divisor)).without_defaults()
      filt_vals = [v for v in values if v % 2 == 0]
      assert_that(result, equal_to([max(filt_vals)]))

  def test_combine_per_key_with_combine_fn(self):
    vals_1 = [1, 2, 3, 4, 5, 6, 7]
    vals_2 = [2, 4, 6, 8, 10, 12, 14]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(
          ([('a', x) for x in vals_1] + [('b', x) for x in vals_2]))
      result = pcoll | 'Mean' >> beam.CombinePerKey(self._MeanCombineFn())
      assert_that(
          result,
          equal_to([('a', sum(vals_1) // len(vals_1)),
                    ('b', sum(vals_2) // len(vals_2))]))

  def test_combine_per_key_with_callable(self):
    vals_1 = [1, 2, 3, 4, 5, 6, 7]
    vals_2 = [2, 4, 6, 8, 10, 12, 14]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(
          ([('a', x) for x in vals_1] + [('b', x) for x in vals_2]))
      result = pcoll | beam.CombinePerKey(sum)
      assert_that(result, equal_to([('a', sum(vals_1)), ('b', sum(vals_2))]))

  def test_combine_per_key_with_side_input_as_arg(self):
    vals_1 = [1, 2, 3, 4, 5, 6, 7]
    vals_2 = [2, 4, 6, 8, 10, 12, 14]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(
          ([('a', x) for x in vals_1] + [('b', x) for x in vals_2]))
      divisor = pipeline | 'Divisor' >> beam.Create([2])
      result = pcoll | beam.CombinePerKey(
          lambda vals, d: max(v for v in vals if v % d == 0),
          pvalue.AsSingleton(divisor))  # Multiples of divisor only.
      m_1 = max(v for v in vals_1 if v % 2 == 0)
      m_2 = max(v for v in vals_2 if v % 2 == 0)
      assert_that(result, equal_to([('a', m_1), ('b', m_2)]))

  def test_group_by_key(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'start' >> beam.Create([(1, 1), (2, 1), (3, 1), (1, 2),
                                                 (2, 2), (1, 3)])
      result = pcoll | 'Group' >> beam.GroupByKey() | SortLists
      assert_that(result, equal_to([(1, [1, 2, 3]), (2, [1, 2]), (3, [1])]))

  def test_group_by_key_unbounded_global_default_trigger(self):
    test_options = PipelineOptions()
    test_options.view_as(TypeOptions).allow_unsafe_triggers = False
    with self.assertRaisesRegex(
        ValueError,
        'GroupByKey cannot be applied to an unbounded PCollection with ' +
        'global windowing and a default trigger'):
      with TestPipeline(options=test_options) as pipeline:
        pipeline | TestStream() | beam.GroupByKey()

  def test_group_by_key_unsafe_trigger(self):
    test_options = PipelineOptions()
    test_options.view_as(TypeOptions).allow_unsafe_triggers = False
    with self.assertRaisesRegex(ValueError, 'Unsafe trigger'):
      with TestPipeline(options=test_options) as pipeline:
        _ = (
            pipeline
            | beam.Create([(None, None)])
            | WindowInto(
                window.GlobalWindows(),
                trigger=trigger.AfterCount(5),
                accumulation_mode=trigger.AccumulationMode.ACCUMULATING)
            | beam.GroupByKey())

  def test_group_by_key_allow_unsafe_triggers(self):
    test_options = PipelineOptions(flags=['--allow_unsafe_triggers'])
    with TestPipeline(options=test_options) as pipeline:
      pcoll = (
          pipeline
          | beam.Create([(1, 1), (1, 2), (1, 3), (1, 4)])
          | WindowInto(
              window.GlobalWindows(),
              trigger=trigger.AfterCount(4),
              accumulation_mode=trigger.AccumulationMode.ACCUMULATING)
          | beam.GroupByKey())
      assert_that(pcoll, equal_to([(1, [1, 2, 3, 4])]))

  def test_group_by_key_reiteration(self):
    class MyDoFn(beam.DoFn):
      def process(self, gbk_result):
        key, value_list = gbk_result
        sum_val = 0
        # Iterate the GBK result for multiple times.
        for _ in range(0, 17):
          sum_val += sum(value_list)
        return [(key, sum_val)]

    with TestPipeline() as pipeline:
      pcoll = pipeline | 'start' >> beam.Create([(1, 1), (1, 2), (1, 3),
                                                 (1, 4)])
      result = (
          pcoll | 'Group' >> beam.GroupByKey()
          | 'Reiteration-Sum' >> beam.ParDo(MyDoFn()))
      assert_that(result, equal_to([(1, 170)]))

  def test_group_by_key_deterministic_coder(self):
    # pylint: disable=global-variable-not-assigned
    global MyObject  # for pickling of the class instance

    class MyObject:
      def __init__(self, value):
        self.value = value

      def __eq__(self, other):
        return self.value == other.value

      def __hash__(self):
        return hash(self.value)

    class MyObjectCoder(beam.coders.Coder):
      def encode(self, o):
        return pickle.dumps((o.value, random.random()))

      def decode(self, encoded):
        return MyObject(pickle.loads(encoded)[0])

      def as_deterministic_coder(self, *args):
        return MydeterministicObjectCoder()

      def to_type_hint(self):
        return MyObject

    class MydeterministicObjectCoder(beam.coders.Coder):
      def encode(self, o):
        return pickle.dumps(o.value)

      def decode(self, encoded):
        return MyObject(pickle.loads(encoded))

      def is_deterministic(self):
        return True

    beam.coders.registry.register_coder(MyObject, MyObjectCoder)

    with TestPipeline() as pipeline:
      pcoll = pipeline | beam.Create([(MyObject(k % 2), k) for k in range(10)])
      grouped = pcoll | beam.GroupByKey() | beam.MapTuple(
          lambda k, vs: (k.value, sorted(vs)))
      combined = pcoll | beam.CombinePerKey(sum) | beam.MapTuple(
          lambda k, v: (k.value, v))
      assert_that(
          grouped,
          equal_to([(0, [0, 2, 4, 6, 8]), (1, [1, 3, 5, 7, 9])]),
          'CheckGrouped')
      assert_that(combined, equal_to([(0, 20), (1, 25)]), 'CheckCombined')

  def test_group_by_key_non_deterministic_coder(self):
    with self.assertRaisesRegex(Exception, r'deterministic'):
      with TestPipeline() as pipeline:
        _ = (
            pipeline
            | beam.Create([(PickledObject(10), None)])
            | beam.GroupByKey()
            | beam.MapTuple(lambda k, v: list(v)))

  def test_group_by_key_allow_non_deterministic_coder(self):
    with TestPipeline() as pipeline:
      # The GroupByKey below would fail without this option.
      pipeline._options.view_as(
          TypeOptions).allow_non_deterministic_key_coders = True
      grouped = (
          pipeline
          | beam.Create([(PickledObject(10), None)])
          | beam.GroupByKey()
          | beam.MapTuple(lambda k, v: list(v)))
      assert_that(grouped, equal_to([[None]]))

  def test_group_by_key_fake_deterministic_coder(self):
    fresh_registry = beam.coders.typecoders.CoderRegistry()
    with patch.object(
        beam.coders, 'registry', fresh_registry), patch.object(
        beam.coders.typecoders, 'registry', fresh_registry):
      with TestPipeline() as pipeline:
        # The GroupByKey below would fail without this registration.
        beam.coders.registry.register_fallback_coder(
            beam.coders.coders.FakeDeterministicFastPrimitivesCoder())
        grouped = (
            pipeline
            | beam.Create([(PickledObject(10), None)])
            | beam.GroupByKey()
            | beam.MapTuple(lambda k, v: list(v)))
        assert_that(grouped, equal_to([[None]]))

  def test_partition_with_partition_fn(self):
    class SomePartitionFn(beam.PartitionFn):
      def partition_for(self, element, num_partitions, offset):
        return (element % 3) + offset

    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([0, 1, 2, 3, 4, 5, 6, 7, 8])
      # Attempt nominal partition operation.
      partitions = pcoll | 'Part 1' >> beam.Partition(SomePartitionFn(), 4, 1)
      assert_that(partitions[0], equal_to([]))
      assert_that(partitions[1], equal_to([0, 3, 6]), label='p1')
      assert_that(partitions[2], equal_to([1, 4, 7]), label='p2')
      assert_that(partitions[3], equal_to([2, 5, 8]), label='p3')

    # Check that a bad partition label will yield an error. For the
    # DirectRunner, this error manifests as an exception.
    with self.assertRaises(Exception):
      with TestPipeline() as pipeline:
        pcoll = pipeline | 'Start' >> beam.Create([0, 1, 2, 3, 4, 5, 6, 7, 8])
        partitions = pcoll | beam.Partition(SomePartitionFn(), 4, 10000)

  def test_partition_with_callable(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([0, 1, 2, 3, 4, 5, 6, 7, 8])
      partitions = (
          pcoll |
          'part' >> beam.Partition(lambda e, n, offset: (e % 3) + offset, 4, 1))
      assert_that(partitions[0], equal_to([]))
      assert_that(partitions[1], equal_to([0, 3, 6]), label='p1')
      assert_that(partitions[2], equal_to([1, 4, 7]), label='p2')
      assert_that(partitions[3], equal_to([2, 5, 8]), label='p3')

  def test_partition_with_callable_and_side_input(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([0, 1, 2, 3, 4, 5, 6, 7, 8])
      side_input = pipeline | 'Side Input' >> beam.Create([100, 1000])
      partitions = (
          pcoll | 'part' >> beam.Partition(
              lambda e, n, offset, si_list: ((e + len(si_list)) % 3) + offset,
              4,
              1,
              pvalue.AsList(side_input)))
      assert_that(partitions[0], equal_to([]))
      assert_that(partitions[1], equal_to([1, 4, 7]), label='p1')
      assert_that(partitions[2], equal_to([2, 5, 8]), label='p2')
      assert_that(partitions[3], equal_to([0, 3, 6]), label='p3')

  def test_partition_followed_by_flatten_and_groupbykey(self):
    """Regression test for an issue with how partitions are handled."""
    with TestPipeline() as pipeline:
      contents = [('aa', 1), ('bb', 2), ('aa', 2)]
      created = pipeline | 'A' >> beam.Create(contents)
      partitioned = created | 'B' >> beam.Partition(lambda x, n: len(x) % n, 3)
      flattened = partitioned | 'C' >> beam.Flatten()
      grouped = flattened | 'D' >> beam.GroupByKey() | SortLists
      assert_that(grouped, equal_to([('aa', [1, 2]), ('bb', [2])]))

  @pytest.mark.it_validatesrunner
  def test_flatten_pcollections(self):
    with TestPipeline() as pipeline:
      pcoll_1 = pipeline | 'Start 1' >> beam.Create([0, 1, 2, 3])
      pcoll_2 = pipeline | 'Start 2' >> beam.Create([4, 5, 6, 7])
      result = (pcoll_1, pcoll_2) | 'Flatten' >> beam.Flatten()
      assert_that(result, equal_to([0, 1, 2, 3, 4, 5, 6, 7]))

  def test_flatten_no_pcollections(self):
    with TestPipeline() as pipeline:
      with self.assertRaises(ValueError):
        () | 'PipelineArgMissing' >> beam.Flatten()
      result = () | 'Empty' >> beam.Flatten(pipeline=pipeline)
      assert_that(result, equal_to([]))

  @pytest.mark.it_validatesrunner
  def test_flatten_one_single_pcollection(self):
    with TestPipeline() as pipeline:
      input = [0, 1, 2, 3]
      pcoll = pipeline | 'Input' >> beam.Create(input)
      result = (pcoll, ) | 'Single Flatten' >> beam.Flatten()
      assert_that(result, equal_to(input))

  # TODO(https://github.com/apache/beam/issues/20067): Does not work in
  # streaming mode on Dataflow.
  @pytest.mark.no_sickbay_streaming
  @pytest.mark.it_validatesrunner
  def test_flatten_same_pcollections(self):
    with TestPipeline() as pipeline:
      pc = pipeline | beam.Create(['a', 'b'])
      assert_that((pc, pc, pc) | beam.Flatten(), equal_to(['a', 'b'] * 3))

  def test_flatten_pcollections_in_iterable(self):
    with TestPipeline() as pipeline:
      pcoll_1 = pipeline | 'Start 1' >> beam.Create([0, 1, 2, 3])
      pcoll_2 = pipeline | 'Start 2' >> beam.Create([4, 5, 6, 7])
      result = [pcoll for pcoll in (pcoll_1, pcoll_2)] | beam.Flatten()
      assert_that(result, equal_to([0, 1, 2, 3, 4, 5, 6, 7]))

  @pytest.mark.it_validatesrunner
  def test_flatten_a_flattened_pcollection(self):
    with TestPipeline() as pipeline:
      pcoll_1 = pipeline | 'Start 1' >> beam.Create([0, 1, 2, 3])
      pcoll_2 = pipeline | 'Start 2' >> beam.Create([4, 5, 6, 7])
      pcoll_3 = pipeline | 'Start 3' >> beam.Create([8, 9])
      pcoll_12 = (pcoll_1, pcoll_2) | 'Flatten' >> beam.Flatten()
      pcoll_123 = (pcoll_12, pcoll_3) | 'Flatten again' >> beam.Flatten()
      assert_that(pcoll_123, equal_to([x for x in range(10)]))

  def test_flatten_input_type_must_be_iterable(self):
    # Inputs to flatten *must* be an iterable.
    with self.assertRaises(ValueError):
      4 | beam.Flatten()

  def test_flatten_input_type_must_be_iterable_of_pcolls(self):
    # Inputs to flatten *must* be an iterable of PCollections.
    with self.assertRaises(TypeError):
      {'l': 'test'} | beam.Flatten()
    with self.assertRaises(TypeError):
      set([1, 2, 3]) | beam.Flatten()

  @pytest.mark.it_validatesrunner
  def test_flatten_multiple_pcollections_having_multiple_consumers(self):
    with TestPipeline() as pipeline:
      input = pipeline | 'Start' >> beam.Create(['AA', 'BBB', 'CC'])

      def split_even_odd(element):
        tag = 'even_length' if len(element) % 2 == 0 else 'odd_length'
        return pvalue.TaggedOutput(tag, element)

      even_length, odd_length = (input | beam.Map(split_even_odd)
                                 .with_outputs('even_length', 'odd_length'))
      merged = (even_length, odd_length) | 'Flatten' >> beam.Flatten()

      assert_that(merged, equal_to(['AA', 'BBB', 'CC']))
      assert_that(even_length, equal_to(['AA', 'CC']), label='assert:even')
      assert_that(odd_length, equal_to(['BBB']), label='assert:odd')

  def test_flatten_with(self):
    with TestPipeline() as pipeline:
      input = pipeline | 'Start' >> beam.Create(['AA', 'BBB', 'CC'])

      result = (
          input
          | 'WithPCollection' >> beam.FlattenWith(input | beam.Map(str.lower))
          | 'WithPTransform' >> beam.FlattenWith(beam.Create(['x', 'y'])))

      assert_that(
          result, equal_to(['AA', 'BBB', 'CC', 'aa', 'bbb', 'cc', 'x', 'y']))

  def test_group_by_key_input_must_be_kv_pairs(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      with TestPipeline() as pipeline:
        pcolls = pipeline | 'A' >> beam.Create([1, 2, 3, 4, 5])
        pcolls | 'D' >> beam.GroupByKey()

    self.assertStartswith(
        e.exception.args[0],
        'Input type hint violation at D: expected '
        'Tuple[TypeVariable[K], TypeVariable[V]]')

  def test_group_by_key_only_input_must_be_kv_pairs(self):
    with self.assertRaises(typehints.TypeCheckError) as cm:
      with TestPipeline() as pipeline:
        pcolls = pipeline | 'A' >> beam.Create(['a', 'b', 'f'])
        pcolls | 'D' >> beam.GroupByKey()

    expected_error_prefix = (
        'Input type hint violation at D: expected '
        'Tuple[TypeVariable[K], TypeVariable[V]]')
    self.assertStartswith(cm.exception.args[0], expected_error_prefix)

  def test_keys_and_values(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([(3, 1), (2, 1), (1, 1), (3, 2),
                                                 (2, 2), (3, 3)])
      keys = pcoll.apply(beam.Keys('keys'))
      vals = pcoll.apply(beam.Values('vals'))
      assert_that(keys, equal_to([1, 2, 2, 3, 3, 3]), label='assert:keys')
      assert_that(vals, equal_to([1, 1, 1, 2, 2, 3]), label='assert:vals')

  def test_kv_swap(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create([(6, 3), (1, 2), (7, 1), (5, 2),
                                                 (3, 2)])
      result = pcoll.apply(beam.KvSwap(), label='swap')
      assert_that(result, equal_to([(1, 7), (2, 1), (2, 3), (2, 5), (3, 6)]))

  def test_distinct(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(
          [6, 3, 1, 1, 9, 'pleat', 'pleat', 'kazoo', 'navel'])
      result = pcoll.apply(beam.Distinct())
      assert_that(result, equal_to([1, 3, 6, 9, 'pleat', 'kazoo', 'navel']))

  def test_chained_ptransforms(self):
    with TestPipeline() as pipeline:
      t = (
          beam.Map(lambda x: (x, 1))
          | beam.GroupByKey()
          | beam.Map(lambda x_ones: (x_ones[0], sum(x_ones[1]))))
      result = pipeline | 'Start' >> beam.Create(['a', 'a', 'b']) | t
      assert_that(result, equal_to([('a', 2), ('b', 1)]))

  def test_apply_to_list(self):
    self.assertCountEqual([1, 2, 3],
                          [0, 1, 2] | 'AddOne' >> beam.Map(lambda x: x + 1))
    self.assertCountEqual([1],
                          [0, 1, 2] | 'Odd' >> beam.Filter(lambda x: x % 2))
    self.assertCountEqual([1, 2, 100, 3], ([1, 2, 3], [100]) | beam.Flatten())
    join_input = ([('k', 'a')], [('k', 'b'), ('k', 'c')])
    self.assertCountEqual([('k', (['a'], ['b', 'c']))],
                          join_input | beam.CoGroupByKey() | SortLists)

  def test_multi_input_ptransform(self):
    class DisjointUnion(PTransform):
      def expand(self, pcollections):
        return (
            pcollections
            | beam.Flatten()
            | beam.Map(lambda x: (x, None))
            | beam.GroupByKey()
            | beam.Map(lambda kv: kv[0]))

    self.assertEqual([1, 2, 3], sorted(([1, 2], [2, 3]) | DisjointUnion()))

  def test_apply_to_crazy_pvaluish(self):
    class NestedFlatten(PTransform):
      """A PTransform taking and returning nested PValueish.

      Takes as input a list of dicts, and returns a dict with the corresponding
      values flattened.
      """
      def _extract_input_pvalues(self, pvalueish):
        pvalueish = list(pvalueish)
        return pvalueish, sum([list(p.values()) for p in pvalueish], [])

      def expand(self, pcoll_dicts):
        keys = reduce(operator.or_, [set(p.keys()) for p in pcoll_dicts])
        res = {}
        for k in keys:
          res[k] = [p[k] for p in pcoll_dicts if k in p] | k >> beam.Flatten()
        return res

    res = [{
        'a': [1, 2, 3]
    }, {
        'a': [4, 5, 6], 'b': ['x', 'y', 'z']
    }, {
        'a': [7, 8], 'b': ['x', 'y'], 'c': []
    }] | NestedFlatten()
    self.assertEqual(3, len(res))
    self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8], sorted(res['a']))
    self.assertEqual(['x', 'x', 'y', 'y', 'z'], sorted(res['b']))
    self.assertEqual([], sorted(res['c']))

  def test_named_tuple(self):
    MinMax = collections.namedtuple('MinMax', ['min', 'max'])

    class MinMaxTransform(PTransform):
      def expand(self, pcoll):
        return MinMax(
            min=pcoll | beam.CombineGlobally(min).without_defaults(),
            max=pcoll | beam.CombineGlobally(max).without_defaults())

    res = [1, 2, 4, 8] | MinMaxTransform()
    self.assertIsInstance(res, MinMax)
    self.assertEqual(res, MinMax(min=[1], max=[8]))

    flat = res | beam.Flatten()
    self.assertEqual(sorted(flat), [1, 8])

  def test_tuple_twice(self):
    class Duplicate(PTransform):
      def expand(self, pcoll):
        return pcoll, pcoll

    res1, res2 = [1, 2, 4, 8] | Duplicate()
    self.assertEqual(sorted(res1), [1, 2, 4, 8])
    self.assertEqual(sorted(res2), [1, 2, 4, 8])

  def test_resource_hint_application_is_additive(self):
    t = beam.Map(lambda x: x + 1).with_resource_hints(
        accelerator='gpu').with_resource_hints(min_ram=1).with_resource_hints(
            accelerator='tpu')
    self.assertEqual(
        t.get_resource_hints(),
        {
            common_urns.resource_hints.ACCELERATOR.urn: b'tpu',
            common_urns.resource_hints.MIN_RAM_BYTES.urn: b'1'
        })


class TestGroupBy(unittest.TestCase):
  def test_lambdas(self):
    def normalize(key, values):
      return tuple(key) if isinstance(key, tuple) else key, sorted(values)

    with TestPipeline() as p:
      pcoll = p | beam.Create(range(6))
      assert_that(
          pcoll | beam.GroupBy() | beam.MapTuple(normalize),
          equal_to([((), [0, 1, 2, 3, 4, 5])]),
          'GroupAll')
      assert_that(
          pcoll | beam.GroupBy(lambda x: x % 2)
          | 'n2' >> beam.MapTuple(normalize),
          equal_to([(0, [0, 2, 4]), (1, [1, 3, 5])]),
          'GroupOne')
      assert_that(
          pcoll | 'G2' >> beam.GroupBy(lambda x: x % 2).force_tuple_keys()
          | 'n3' >> beam.MapTuple(normalize),
          equal_to([((0, ), [0, 2, 4]), ((1, ), [1, 3, 5])]),
          'GroupOneTuple')
      assert_that(
          pcoll | beam.GroupBy(a=lambda x: x % 2, b=lambda x: x < 4)
          | 'n4' >> beam.MapTuple(normalize),
          equal_to([((0, True), [0, 2]), ((1, True), [1, 3]), ((0, False), [4]),
                    ((1, False), [5])]),
          'GroupTwo')

  def test_fields(self):
    def normalize(key, values):
      if isinstance(key, tuple):
        key = beam.Row(
            **{
                name: value
                for name, value in zip(type(key)._fields, key)
            })
      return key, sorted(v.value for v in values)

    with TestPipeline() as p:
      pcoll = p | beam.Create(range(-2, 3)) | beam.Map(int) | beam.Map(
          lambda x: beam.Row(
              value=x, square=x * x, sign=x // abs(x) if x else 0))
      assert_that(
          pcoll | beam.GroupBy('square') | beam.MapTuple(normalize),
          equal_to([
              (0, [0]),
              (1, [-1, 1]),
              (4, [-2, 2]),
          ]),
          'GroupSquare')
      assert_that(
          pcoll | 'G2' >> beam.GroupBy('square').force_tuple_keys()
          | 'n2' >> beam.MapTuple(normalize),
          equal_to([
              (beam.Row(square=0), [0]),
              (beam.Row(square=1), [-1, 1]),
              (beam.Row(square=4), [-2, 2]),
          ]),
          'GroupSquareTupleKey')
      assert_that(
          pcoll | beam.GroupBy('square', 'sign')
          | 'n3' >> beam.MapTuple(normalize),
          equal_to([
              (beam.Row(square=0, sign=0), [0]),
              (beam.Row(square=1, sign=1), [1]),
              (beam.Row(square=4, sign=1), [2]),
              (beam.Row(square=1, sign=-1), [-1]),
              (beam.Row(square=4, sign=-1), [-2]),
          ]),
          'GroupSquareSign')
      assert_that(
          pcoll | beam.GroupBy('square', big=lambda x: x.value > 1)
          | 'n4' >> beam.MapTuple(normalize),
          equal_to([
              (beam.Row(square=0, big=False), [0]),
              (beam.Row(square=1, big=False), [-1, 1]),
              (beam.Row(square=4, big=False), [-2]),
              (beam.Row(square=4, big=True), [2]),
          ]),
          'GroupSquareNonzero')

  def test_aggregate(self):
    def named_tuple_to_row(t):
      return beam.Row(
          **{
              name: value
              for name, value in zip(type(t)._fields, t)
          })

    with TestPipeline() as p:
      pcoll = p | beam.Create(range(-2, 3)) | beam.Map(
          lambda x: beam.Row(
              value=x, square=x * x, sign=x // abs(x) if x else 0))

      assert_that(
          pcoll
          | beam.GroupBy('square', big=lambda x: x.value > 1).aggregate_field(
              'value', sum, 'sum').aggregate_field(
                  lambda x: x.sign == 1, all, 'positive')
          | beam.Map(named_tuple_to_row),
          equal_to([
              beam.Row(square=0, big=False, sum=0, positive=False),  # [0],
              beam.Row(square=1, big=False, sum=0, positive=False),  # [-1, 1]
              beam.Row(square=4, big=False, sum=-2, positive=False),  # [-2]
              beam.Row(square=4, big=True, sum=2, positive=True),  # [2]
          ]))

  def test_pickled_field(self):
    with TestPipeline() as p:
      assert_that(
          p
          | beam.Create(['a', 'a', 'b'])
          | beam.Map(
              lambda s: beam.Row(
                  key1=PickledObject(s), key2=s.upper(), value=0))
          | beam.GroupBy('key1', 'key2')
          | beam.MapTuple(lambda k, vs: (k.key1.value, k.key2, len(list(vs)))),
          equal_to([('a', 'A', 2), ('b', 'B', 1)]))


class SelectTest(unittest.TestCase):
  def test_simple(self):
    with TestPipeline() as p:
      rows = (
          p | beam.Create([1, 2, 10])
          | beam.Select(a=lambda x: x * x, b=lambda x: -x))

      assert_that(
          rows,
          equal_to([
              beam.Row(a=1, b=-1),
              beam.Row(a=4, b=-2),
              beam.Row(a=100, b=-10),
          ]),
          label='CheckFromLambdas')

      from_attr = rows | beam.Select('b', z='a')
      assert_that(
          from_attr,
          equal_to([
              beam.Row(b=-1, z=1),
              beam.Row(b=-2, z=4),
              beam.Row(
                  b=-10,
                  z=100,
              ),
          ]),
          label='CheckFromAttrs')

  def test_type_inference(self):
    with TestPipeline() as p:
      input_rows = p | beam.Create([beam.Row(s='abc', i=1)])
      output_rows = input_rows | beam.Select(
          's', 'i', s_again='s', expr=lambda x: x.i + 1)
      field_types = dict(output_rows.element_type._fields)
      self.assertEqual(field_types['s'], str)
      self.assertEqual(field_types['i'], int)
      self.assertEqual(field_types['s_again'], str)
      self.assertEqual(field_types['expr'], int)


@beam.ptransform_fn
def SamplePTransform(pcoll):
  """Sample transform using the @ptransform_fn decorator."""
  map_transform = 'ToPairs' >> beam.Map(lambda v: (v, None))
  combine_transform = 'Group' >> beam.CombinePerKey(lambda vs: None)
  keys_transform = 'Distinct' >> beam.Keys()
  return pcoll | map_transform | combine_transform | keys_transform


class PTransformLabelsTest(unittest.TestCase):
  class CustomTransform(beam.PTransform):

    pardo: Optional[beam.PTransform] = None

    def expand(self, pcoll):
      self.pardo = '*Do*' >> beam.FlatMap(lambda x: [x + 1])
      return pcoll | self.pardo

  def test_chained_ptransforms(self):
    """Tests that chaining gets proper nesting."""
    with TestPipeline() as pipeline:
      map1 = 'Map1' >> beam.Map(lambda x: (x, 1))
      gbk = 'Gbk' >> beam.GroupByKey()
      map2 = 'Map2' >> beam.Map(lambda x_ones2: (x_ones2[0], sum(x_ones2[1])))
      t = (map1 | gbk | map2)
      result = pipeline | 'Start' >> beam.Create(['a', 'a', 'b']) | t
      self.assertTrue('Map1|Gbk|Map2/Map1' in pipeline.applied_labels)
      self.assertTrue('Map1|Gbk|Map2/Gbk' in pipeline.applied_labels)
      self.assertTrue('Map1|Gbk|Map2/Map2' in pipeline.applied_labels)
      assert_that(result, equal_to([('a', 2), ('b', 1)]))

  def test_apply_custom_transform_without_label(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'PColl' >> beam.Create([1, 2, 3])
      custom = PTransformLabelsTest.CustomTransform()
      result = pipeline.apply(custom, pcoll)
      self.assertTrue('CustomTransform' in pipeline.applied_labels)
      self.assertTrue('CustomTransform/*Do*' in pipeline.applied_labels)
      assert_that(result, equal_to([2, 3, 4]))

  def test_apply_custom_transform_with_label(self):
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'PColl' >> beam.Create([1, 2, 3])
      custom = PTransformLabelsTest.CustomTransform('*Custom*')
      result = pipeline.apply(custom, pcoll)
      self.assertTrue('*Custom*' in pipeline.applied_labels)
      self.assertTrue('*Custom*/*Do*' in pipeline.applied_labels)
      assert_that(result, equal_to([2, 3, 4]))

  def test_combine_without_label(self):
    vals = [1, 2, 3, 4, 5, 6, 7]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(vals)
      combine = beam.CombineGlobally(sum)
      result = pcoll | combine
      self.assertTrue('CombineGlobally(sum)' in pipeline.applied_labels)
      assert_that(result, equal_to([sum(vals)]))

  def test_apply_ptransform_using_decorator(self):
    pipeline = TestPipeline()
    pcoll = pipeline | 'PColl' >> beam.Create([1, 2, 3])
    _ = pcoll | '*Sample*' >> SamplePTransform()
    self.assertTrue('*Sample*' in pipeline.applied_labels)
    self.assertTrue('*Sample*/ToPairs' in pipeline.applied_labels)
    self.assertTrue('*Sample*/Group' in pipeline.applied_labels)
    self.assertTrue('*Sample*/Distinct' in pipeline.applied_labels)

  def test_combine_with_label(self):
    vals = [1, 2, 3, 4, 5, 6, 7]
    with TestPipeline() as pipeline:
      pcoll = pipeline | 'Start' >> beam.Create(vals)
      combine = '*Sum*' >> beam.CombineGlobally(sum)
      result = pcoll | combine
      self.assertTrue('*Sum*' in pipeline.applied_labels)
      assert_that(result, equal_to([sum(vals)]))

  def check_label(self, ptransform, expected_label):
    pipeline = TestPipeline()
    pipeline | 'Start' >> beam.Create([('a', 1)]) | ptransform
    actual_label = sorted(
        label for label in pipeline.applied_labels
        if not label.startswith('Start'))[0]
    self.assertEqual(expected_label, re.sub(r'\d{3,}', '#', actual_label))

  def test_default_labels(self):
    def my_function(*args):
      pass

    self.check_label(beam.Map(len), 'Map(len)')
    self.check_label(beam.Map(my_function), 'Map(my_function)')
    self.check_label(
        beam.Map(lambda x: x), 'Map(<lambda at ptransform_test.py:#>)')
    self.check_label(beam.FlatMap(list), 'FlatMap(list)')
    self.check_label(beam.FlatMap(my_function), 'FlatMap(my_function)')
    self.check_label(beam.Filter(sum), 'Filter(sum)')
    self.check_label(beam.CombineGlobally(sum), 'CombineGlobally(sum)')
    self.check_label(beam.CombinePerKey(sum), 'CombinePerKey(sum)')

    class MyDoFn(beam.DoFn):
      def process(self, unused_element):
        pass

    self.check_label(beam.ParDo(MyDoFn()), 'ParDo(MyDoFn)')

  def test_label_propogation(self):
    self.check_label('TestMap' >> beam.Map(len), 'TestMap')
    self.check_label('TestLambda' >> beam.Map(lambda x: x), 'TestLambda')
    self.check_label('TestFlatMap' >> beam.FlatMap(list), 'TestFlatMap')
    self.check_label('TestFilter' >> beam.Filter(sum), 'TestFilter')
    self.check_label('TestCG' >> beam.CombineGlobally(sum), 'TestCG')
    self.check_label('TestCPK' >> beam.CombinePerKey(sum), 'TestCPK')

    class MyDoFn(beam.DoFn):
      def process(self, unused_element):
        pass

    self.check_label('TestParDo' >> beam.ParDo(MyDoFn()), 'TestParDo')


class PTransformTestDisplayData(unittest.TestCase):
  def test_map_named_function(self):
    tr = beam.Map(len)
    dd = DisplayData.create_from(tr)
    nspace = 'apache_beam.transforms.core.CallableWrapperDoFn'
    expected_item = DisplayDataItem(
        'len', key='fn', label='Transform Function', namespace=nspace)
    hc.assert_that(dd.items, hc.has_item(expected_item))

  def test_map_anonymous_function(self):
    tr = beam.Map(lambda x: x)
    dd = DisplayData.create_from(tr)
    nspace = 'apache_beam.transforms.core.CallableWrapperDoFn'
    expected_item = DisplayDataItem(
        '<lambda>', key='fn', label='Transform Function', namespace=nspace)
    hc.assert_that(dd.items, hc.has_item(expected_item))

  def test_flatmap_named_function(self):
    tr = beam.FlatMap(list)
    dd = DisplayData.create_from(tr)
    nspace = 'apache_beam.transforms.core.CallableWrapperDoFn'
    expected_item = DisplayDataItem(
        'list', key='fn', label='Transform Function', namespace=nspace)
    hc.assert_that(dd.items, hc.has_item(expected_item))

  def test_flatmap_anonymous_function(self):
    tr = beam.FlatMap(lambda x: [x])
    dd = DisplayData.create_from(tr)
    nspace = 'apache_beam.transforms.core.CallableWrapperDoFn'
    expected_item = DisplayDataItem(
        '<lambda>', key='fn', label='Transform Function', namespace=nspace)
    hc.assert_that(dd.items, hc.has_item(expected_item))

  def test_filter_named_function(self):
    tr = beam.Filter(sum)
    dd = DisplayData.create_from(tr)
    nspace = 'apache_beam.transforms.core.CallableWrapperDoFn'
    expected_item = DisplayDataItem(
        'sum', key='fn', label='Transform Function', namespace=nspace)
    hc.assert_that(dd.items, hc.has_item(expected_item))

  def test_filter_anonymous_function(self):
    tr = beam.Filter(lambda x: x // 30)
    dd = DisplayData.create_from(tr)
    nspace = 'apache_beam.transforms.core.CallableWrapperDoFn'
    expected_item = DisplayDataItem(
        '<lambda>', key='fn', label='Transform Function', namespace=nspace)
    hc.assert_that(dd.items, hc.has_item(expected_item))


class PTransformTypeCheckTestCase(TypeHintTestCase):
  def assertStartswith(self, msg, prefix):
    self.assertTrue(
        msg.startswith(prefix), '"%s" does not start with "%s"' % (msg, prefix))

  def setUp(self):
    self.p = TestPipeline()

  def test_do_fn_pipeline_pipeline_type_check_satisfied(self):
    @with_input_types(int, int)
    @with_output_types(int)
    class AddWithFive(beam.DoFn):
      def process(self, element, five):
        return [element + five]

    d = (
        self.p
        | 'T' >> beam.Create([1, 2, 3]).with_output_types(int)
        | 'Add' >> beam.ParDo(AddWithFive(), 5))

    assert_that(d, equal_to([6, 7, 8]))
    self.p.run()

  def test_do_fn_pipeline_pipeline_type_check_violated(self):
    @with_input_types(str, str)
    @with_output_types(str)
    class ToUpperCaseWithPrefix(beam.DoFn):
      def process(self, element, prefix):
        return [prefix + element.upper()]

    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Upper.*requires.*str.*applied.*int'):
      (
          self.p
          | 'T' >> beam.Create([1, 2, 3]).with_output_types(int)
          | 'Upper' >> beam.ParDo(ToUpperCaseWithPrefix(), 'hello'))

  def test_do_fn_pipeline_runtime_type_check_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    @with_input_types(int, int)
    @with_output_types(int)
    class AddWithNum(beam.DoFn):
      def process(self, element, num):
        return [element + num]

    d = (
        self.p
        | 'T' >> beam.Create([1, 2, 3]).with_output_types(int)
        | 'Add' >> beam.ParDo(AddWithNum(), 5))

    assert_that(d, equal_to([6, 7, 8]))
    self.p.run()

  def test_do_fn_pipeline_runtime_type_check_violated(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    @with_input_types(int, int)
    @with_output_types(int)
    class AddWithNum(beam.DoFn):
      def process(self, element, num):
        return [element + num]

    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Add.*requires.*int.*applied.*str'):
      (
          self.p
          | 'T' >> beam.Create(['1', '2', '3']).with_output_types(str)
          | 'Add' >> beam.ParDo(AddWithNum(), 5))
      self.p.run()

  def test_pardo_does_not_type_check_using_type_hint_decorators(self):
    @with_input_types(a=int)
    @with_output_types(typing.List[str])
    def int_to_str(a):
      return [str(a)]

    # The function above is expecting an int for its only parameter. However, it
    # will receive a str instead, which should result in a raised exception.
    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'ToStr.*requires.*int.*applied.*str'):
      (
          self.p
          | 'S' >> beam.Create(['b', 'a', 'r']).with_output_types(str)
          | 'ToStr' >> beam.FlatMap(int_to_str))

  def test_pardo_properly_type_checks_using_type_hint_decorators(self):
    @with_input_types(a=str)
    @with_output_types(typing.List[str])
    def to_all_upper_case(a):
      return [a.upper()]

    # If this type-checks than no error should be raised.
    d = (
        self.p
        | 'T' >> beam.Create(['t', 'e', 's', 't']).with_output_types(str)
        | 'Case' >> beam.FlatMap(to_all_upper_case))
    assert_that(d, equal_to(['T', 'E', 'S', 'T']))
    self.p.run()

    # Output type should have been recognized as 'str' rather than List[str] to
    # do the flatten part of FlatMap.
    self.assertEqual(str, d.element_type)

  def test_pardo_does_not_type_check_using_type_hint_methods(self):
    # The first ParDo outputs pcoll's of type int, however the second ParDo is
    # expecting pcoll's of type str instead.
    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Upper.*requires.*str.*applied.*int'):
      (
          self.p
          | 'S' >> beam.Create(['t', 'e', 's', 't']).with_output_types(str)
          | (
              'Score' >> beam.FlatMap(lambda x: [1] if x == 't' else [2]).
              with_input_types(str).with_output_types(int))
          | (
              'Upper' >> beam.FlatMap(lambda x: [x.upper()]).with_input_types(
                  str).with_output_types(str)))

  def test_pardo_properly_type_checks_using_type_hint_methods(self):
    # Pipeline should be created successfully without an error
    d = (
        self.p
        | 'S' >> beam.Create(['t', 'e', 's', 't']).with_output_types(str)
        | 'Dup' >> beam.FlatMap(lambda x: [x + x]).with_input_types(
            str).with_output_types(str)
        | 'Upper' >> beam.FlatMap(lambda x: [x.upper()]).with_input_types(
            str).with_output_types(str))

    assert_that(d, equal_to(['TT', 'EE', 'SS', 'TT']))
    self.p.run()

  def test_map_does_not_type_check_using_type_hints_methods(self):
    # The transform before 'Map' has indicated that it outputs PCollections with
    # int's, while Map is expecting one of str.
    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Upper.*requires.*str.*applied.*int'):
      (
          self.p
          | 'S' >> beam.Create([1, 2, 3, 4]).with_output_types(int)
          | 'Upper' >> beam.Map(lambda x: x.upper()).with_input_types(
              str).with_output_types(str))

  def test_map_properly_type_checks_using_type_hints_methods(self):
    # No error should be raised if this type-checks properly.
    d = (
        self.p
        | 'S' >> beam.Create([1, 2, 3, 4]).with_output_types(int)
        | 'ToStr' >>
        beam.Map(lambda x: str(x)).with_input_types(int).with_output_types(str))
    assert_that(d, equal_to(['1', '2', '3', '4']))
    self.p.run()

  def test_map_does_not_type_check_using_type_hints_decorator(self):
    @with_input_types(s=str)
    @with_output_types(str)
    def upper(s):
      return s.upper()

    # Hinted function above expects a str at pipeline construction.
    # However, 'Map' should detect that Create has hinted an int instead.
    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Upper.*requires.*str.*applied.*int'):
      (
          self.p
          | 'S' >> beam.Create([1, 2, 3, 4]).with_output_types(int)
          | 'Upper' >> beam.Map(upper))

  def test_map_properly_type_checks_using_type_hints_decorator(self):
    @with_input_types(a=bool)
    @with_output_types(int)
    def bool_to_int(a):
      return int(a)

    # If this type-checks than no error should be raised.
    d = (
        self.p
        | 'Bools' >> beam.Create([True, False, True]).with_output_types(bool)
        | 'ToInts' >> beam.Map(bool_to_int))
    assert_that(d, equal_to([1, 0, 1]))
    self.p.run()

  def test_filter_does_not_type_check_using_type_hints_method(self):
    # Filter is expecting an int but instead looks to the 'left' and sees a str
    # incoming.
    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Below 3.*requires.*int.*applied.*str'):
      (
          self.p
          | 'Strs' >> beam.Create(['1', '2', '3', '4', '5'
                                   ]).with_output_types(str)
          | 'Lower' >> beam.Map(lambda x: x.lower()).with_input_types(
              str).with_output_types(str)
          | 'Below 3' >> beam.Filter(lambda x: x < 3).with_input_types(int))

  def test_filter_type_checks_using_type_hints_method(self):
    # No error should be raised if this type-checks properly.
    d = (
        self.p
        | beam.Create(['1', '2', '3', '4', '5']).with_output_types(str)
        | 'ToInt' >>
        beam.Map(lambda x: int(x)).with_input_types(str).with_output_types(int)
        | 'Below 3' >> beam.Filter(lambda x: x < 3).with_input_types(int))
    assert_that(d, equal_to([1, 2]))
    self.p.run()

  def test_filter_does_not_type_check_using_type_hints_decorator(self):
    @with_input_types(a=float)
    def more_than_half(a):
      return a > 0.50

    # Func above was hinted to only take a float, yet a str will be passed.
    with self.assertRaisesRegex(typehints.TypeCheckError,
                                r'Half.*requires.*float.*applied.*str'):
      (
          self.p
          | 'Ints' >> beam.Create(['1', '2', '3', '4']).with_output_types(str)
          | 'Half' >> beam.Filter(more_than_half))

  def test_filter_type_checks_using_type_hints_decorator(self):
    @with_input_types(b=int)
    def half(b):
      return bool(random.choice([0, 1]))

    # Filter should deduce that it returns the same type that it takes.
    (
        self.p
        | 'Str' >> beam.Create(range(5)).with_output_types(int)
        | 'Half' >> beam.Filter(half)
        | 'ToBool' >> beam.Map(lambda x: bool(x)).with_input_types(
            int).with_output_types(bool))

  def test_pardo_like_inheriting_output_types_from_annotation(self):
    def fn1(x: str) -> int:
      return 1

    def fn1_flat(x: str) -> typing.List[int]:
      return [1]

    def fn2(x: int, y: str) -> str:
      return y

    def fn2_flat(x: int, y: str) -> typing.List[str]:
      return [y]

    # We only need the args section of the hints.
    def output_hints(transform):
      return transform.default_type_hints().output_types[0][0]

    self.assertEqual(int, output_hints(beam.Map(fn1)))
    self.assertEqual(int, output_hints(beam.FlatMap(fn1_flat)))

    self.assertEqual(str, output_hints(beam.MapTuple(fn2)))
    self.assertEqual(str, output_hints(beam.FlatMapTuple(fn2_flat)))

    def add(a: typing.Iterable[int]) -> int:
      return sum(a)

    self.assertCompatible(
        typing.Tuple[typing.TypeVar('K'), int],
        output_hints(beam.CombinePerKey(add)))

  def test_group_by_key_only_output_type_deduction(self):
    d = (
        self.p
        | 'Str' >> beam.Create(['t', 'e', 's', 't']).with_output_types(str)
        | (
            'Pair' >> beam.Map(lambda x: (x, ord(x))).with_output_types(
                typing.Tuple[str, str]))
        | beam.GroupByKey())

    # Output type should correctly be deduced.
    # GBK-only should deduce that Tuple[A, B] is turned into
    # Tuple[A, Iterable[B]].
    self.assertCompatible(
        typing.Tuple[str, typing.Iterable[str]], d.element_type)

  def test_group_by_key_output_type_deduction(self):
    d = (
        self.p
        | 'Str' >> beam.Create(range(20)).with_output_types(int)
        | (
            'PairNegative' >> beam.Map(lambda x: (x % 5, -x)).with_output_types(
                typing.Tuple[int, int]))
        | beam.GroupByKey())

    # Output type should correctly be deduced.
    # GBK should deduce that Tuple[A, B] is turned into Tuple[A, Iterable[B]].
    self.assertCompatible(
        typing.Tuple[int, typing.Iterable[int]], d.element_type)

  def test_group_by_key_only_does_not_type_check(self):
    # GBK will be passed raw int's here instead of some form of Tuple[A, B].
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | beam.Create([1, 2, 3]).with_output_types(int)
          | 'F' >> beam.GroupByKey())

    self.assertStartswith(
        e.exception.args[0],
        "Input type hint violation at F: "
        "expected Tuple[TypeVariable[K], TypeVariable[V]], "
        "got {}".format(int))

  def test_group_by_does_not_type_check(self):
    # Create is returning a List[int, str], rather than a Tuple[int, str]
    # that is aliased to Tuple[int, str].
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | (beam.Create([[1], [2]]).with_output_types(typing.Iterable[int]))
          | 'T' >> beam.GroupByKey())

    self.assertStartswith(
        e.exception.args[0],
        "Input type hint violation at T: "
        "expected Tuple[TypeVariable[K], TypeVariable[V]], "
        "got Iterable[<class 'int'>]")

  def test_pipeline_checking_pardo_insufficient_type_information(self):
    self.p._options.view_as(TypeOptions).type_check_strictness = 'ALL_REQUIRED'

    # Type checking is enabled, but 'Create' doesn't pass on any relevant type
    # information to the ParDo.
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | 'Nums' >> beam.Create(range(5))
          | 'ModDup' >> beam.FlatMap(lambda x: (x % 2, x)))

    self.assertEqual(
        'Pipeline type checking is enabled, however no output '
        'type-hint was found for the PTransform Create(Nums)',
        e.exception.args[0])

  def test_pipeline_checking_gbk_insufficient_type_information(self):
    self.p._options.view_as(TypeOptions).type_check_strictness = 'ALL_REQUIRED'
    # Type checking is enabled, but 'Map' doesn't pass on any relevant type
    # information to GBK-only.
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | 'Nums' >> beam.Create(range(5)).with_output_types(int)
          | 'ModDup' >> beam.Map(lambda x: (x % 2, x))
          | beam.GroupByKey())

    self.assertEqual(
        'Pipeline type checking is enabled, however no output '
        'type-hint was found for the PTransform '
        'ParDo(ModDup)',
        e.exception.args[0])

  def test_disable_pipeline_type_check(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    # The pipeline below should raise a TypeError, however pipeline type
    # checking was disabled above.
    (
        self.p
        | 'T' >> beam.Create([1, 2, 3]).with_output_types(int)
        | 'Lower' >> beam.Map(lambda x: x.lower()).with_input_types(
            str).with_output_types(str))

  def test_run_time_type_checking_enabled_type_violation(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    @with_output_types(str)
    @with_input_types(x=int)
    def int_to_string(x):
      return str(x)

    # Function above has been type-hinted to only accept an int. But in the
    # pipeline execution it'll be passed a string due to the output of Create.
    (
        self.p
        | 'T' >> beam.Create(['some_string'])
        | 'ToStr' >> beam.Map(int_to_string))
    error_regex = "Type-hint for argument: 'x' violated. Expected an instance "
    "of {}, instead found some_string, an instance of {}.".format(int, str)

    with self.assertRaisesRegex(Exception, error_regex):
      self.p.run()

  def test_run_time_type_checking_enabled_types_satisfied(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    @with_output_types(typing.Tuple[int, str])
    @with_input_types(x=str)
    def group_with_upper_ord(x):
      return (ord(x.upper()) % 5, x)

    # Pipeline checking is off, but the above function should satisfy types at
    # run-time.
    result = (
        self.p
        | 'T' >> beam.Create(['t', 'e', 's', 't', 'i', 'n', 'g'
                              ]).with_output_types(str)
        | 'GenKeys' >> beam.Map(group_with_upper_ord)
        | 'O' >> beam.GroupByKey()
        | SortLists)

    assert_that(
        result,
        equal_to([(1, ['g']), (3, ['i', 'n', 's']), (4, ['e', 't', 't'])]))
    self.p.run()

  def test_pipeline_checking_satisfied_but_run_time_types_violate(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    @with_output_types(typing.Tuple[bool, int])
    @with_input_types(a=int)
    def is_even_as_key(a):
      # Simulate a programming error, should be: return (a % 2 == 0, a)
      # However this returns Tuple[int, int]
      return (a % 2, a)

    (
        self.p
        | 'Nums' >> beam.Create(range(5)).with_output_types(int)
        | 'IsEven' >> beam.Map(is_even_as_key)
        | 'Parity' >> beam.GroupByKey())

    # Although all the types appear to be correct when checked at pipeline
    # construction. Runtime type-checking should detect the 'is_even_as_key' is
    # returning Tuple[int, int], instead of Tuple[bool, int].
    error_regex = "Runtime type violation detected"

    with self.assertRaisesRegex(Exception, error_regex):
      self.p.run()

  def test_pipeline_checking_satisfied_run_time_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    @with_output_types(typing.Tuple[bool, int])
    @with_input_types(a=int)
    def is_even_as_key(a):
      # The programming error in the above test-case has now been fixed.
      # Everything should properly type-check.
      return (a % 2 == 0, a)

    result = (
        self.p
        | 'Nums' >> beam.Create(range(5)).with_output_types(int)
        | 'IsEven' >> beam.Map(is_even_as_key)
        | 'Parity' >> beam.GroupByKey()
        | SortLists)

    assert_that(result, equal_to([(False, [1, 3]), (True, [0, 2, 4])]))
    self.p.run()

  def test_pipeline_runtime_checking_violation_simple_type_input(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    # The type-hinted applied via the 'with_input_types()' method indicates the
    # ParDo should receive an instance of type 'str', however an 'int' will be
    # passed instead.
    error_regex = "Runtime type violation detected"

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([1, 1, 1])
          | (
              'ToInt' >> beam.FlatMap(lambda x: [int(x)]).with_input_types(
                  str).with_output_types(int)))
      self.p.run()

  def test_pipeline_runtime_checking_violation_composite_type_input(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    error_regex = "Runtime type violation detected"

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([(1, 3.0), (2, 4.9), (3, 9.5)])
          | (
              'Add' >>
              beam.FlatMap(lambda x_y: [x_y[0] + x_y[1]]).with_input_types(
                  typing.Tuple[int, int]).with_output_types(int)))
      self.p.run()

  def test_pipeline_runtime_checking_violation_simple_type_output(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    # The type-hinted applied via the 'returns()' method indicates the ParDo
    # should output an instance of type 'int', however a 'float' will be
    # generated instead.
    print(
        "HINTS",
        (
            'ToInt' >> beam.FlatMap(lambda x: [float(x)]).with_input_types(
                int).with_output_types(int)).get_type_hints())
    error_regex = ""

    if self.p._options.view_as(TypeOptions).runtime_type_check:
      error_regex = (
          "Runtime type violation detected within ParDo\\(ToInt\\):" +
          " According to type-hint expected output should be of type <class " +
          "'int'>. Instead, received '1.0', an instance of type <class 'float'>"
      )

    if self.p._options.view_as(TypeOptions).performance_runtime_type_check:
      error_regex = (
          "Runtime type violation detected within ToInt: Type-hint " +
          "for argument: 'x' violated. Expected an instance of <class 'int'>, "
          + "instead found 1.0, an instance of <class 'float'>")

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([1, 1, 1])
          | (
              'ToInt' >> beam.FlatMap(lambda x: [float(x)]).with_input_types(
                  int).with_output_types(int)))
      self.p.run().wait_until_finish()

  def test_pipeline_runtime_checking_violation_composite_type_output(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    # The type-hinted applied via the 'returns()' method indicates the ParDo
    # should return an instance of type: Tuple[float, int]. However, an instance
    # of 'int' will be generated instead.
    error_regex = (
        "Runtime type violation detected within " +
        "ParDo\\(Swap\\): Tuple type constraint violated. " +
        "Valid object instance must be of type 'tuple'. Instead, " +
        "an instance of 'float' was received.")

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([(1, 3.0), (2, 4.9), (3, 9.5)])
          | (
              'Swap' >>
              beam.FlatMap(lambda x_y1: [x_y1[0] + x_y1[1]]).with_input_types(
                  typing.Tuple[int, float]).with_output_types(
                      typing.Tuple[float, int])))
      self.p.run()

  def test_pipeline_runtime_checking_violation_with_side_inputs_decorator(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    @with_output_types(int)
    @with_input_types(a=int, b=int)
    def add(a, b):
      return a + b

    error_regex = "Runtime type violation detected"

    with self.assertRaisesRegex(Exception, error_regex):
      (self.p | beam.Create([1, 2, 3, 4]) | 'Add 1' >> beam.Map(add, 1.0))
      self.p.run()

  def test_pipeline_runtime_checking_violation_with_side_inputs_via_method(self):  # pylint: disable=line-too-long
    self.p._options.view_as(TypeOptions).runtime_type_check = True
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    error_regex = "Runtime type violation detected"
    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([1, 2, 3, 4])
          | (
              'Add 1' >> beam.Map(lambda x, one: x + one, 1.0).with_input_types(
                  int, int).with_output_types(float)))
      self.p.run()

  def test_combine_properly_pipeline_type_checks_using_decorator(self):
    @with_output_types(int)
    @with_input_types(ints=typing.Iterable[int])
    def sum_ints(ints):
      return sum(ints)

    d = (
        self.p
        | 'T' >> beam.Create([1, 2, 3]).with_output_types(int)
        | 'Sum' >> beam.CombineGlobally(sum_ints))

    self.assertEqual(int, d.element_type)
    assert_that(d, equal_to([6]))
    self.p.run()

  def test_combine_properly_pipeline_type_checks_without_decorator(self):
    def sum_ints(ints):
      return sum(ints)

    d = (
        self.p
        | beam.Create([1, 2, 3])
        | beam.Map(lambda x: ('key', x))
        | beam.CombinePerKey(sum_ints))

    self.assertEqual(typehints.Tuple[str, typehints.Any], d.element_type)
    self.p.run()

  def test_combine_func_type_hint_does_not_take_iterable_using_decorator(self):
    @with_output_types(int)
    @with_input_types(a=int)
    def bad_combine(a):
      5 + a

    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | 'M' >> beam.Create([1, 2, 3]).with_output_types(int)
          | 'Add' >> beam.CombineGlobally(bad_combine))

    self.assertEqual(
        "All functions for a Combine PTransform must accept a "
        "single argument compatible with: Iterable[Any]. "
        "Instead a function with input type: {} was received.".format(int),
        e.exception.args[0])

  def test_combine_pipeline_type_propagation_using_decorators(self):
    @with_output_types(int)
    @with_input_types(ints=typing.Iterable[int])
    def sum_ints(ints):
      return sum(ints)

    @with_output_types(typing.List[int])
    @with_input_types(n=int)
    def range_from_zero(n):
      return list(range(n + 1))

    d = (
        self.p
        | 'T' >> beam.Create([1, 2, 3]).with_output_types(int)
        | 'Sum' >> beam.CombineGlobally(sum_ints)
        | 'Range' >> beam.ParDo(range_from_zero))

    self.assertEqual(int, d.element_type)
    assert_that(d, equal_to([0, 1, 2, 3, 4, 5, 6]))
    self.p.run()

  def test_combine_runtime_type_check_satisfied_using_decorators(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False

    @with_output_types(int)
    @with_input_types(ints=typing.Iterable[int])
    def iter_mul(ints):
      return reduce(operator.mul, ints, 1)

    d = (
        self.p
        | 'K' >> beam.Create([5, 5, 5, 5]).with_output_types(int)
        | 'Mul' >> beam.CombineGlobally(iter_mul))

    assert_that(d, equal_to([625]))
    self.p.run()

  def test_combine_runtime_type_check_violation_using_decorators(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    # Combine fn is returning the incorrect type
    @with_output_types(int)
    @with_input_types(ints=typing.Iterable[int])
    def iter_mul(ints):
      return str(reduce(operator.mul, ints, 1))

    error_regex = (
        "Runtime type violation detected within " +
        "Mul/CombinePerKey: Type-hint for return type violated. " +
        "Expected an instance of {}, instead found".format(int))

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | 'K' >> beam.Create([5, 5, 5, 5]).with_output_types(int)
          | 'Mul' >> beam.CombineGlobally(iter_mul))
      self.p.run()

  def test_combine_pipeline_type_check_using_methods(self):
    d = (
        self.p
        | beam.Create(['t', 'e', 's', 't']).with_output_types(str)
        | (
            'concat' >> beam.CombineGlobally(lambda s: ''.join(s)).
            with_input_types(str).with_output_types(str)))

    def matcher(expected):
      def match(actual):
        equal_to(expected)(list(actual[0]))

      return match

    assert_that(d, matcher('estt'))
    self.p.run()

  def test_combine_runtime_type_check_using_methods(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create(range(5)).with_output_types(int)
        | (
            'Sum' >> beam.CombineGlobally(lambda s: sum(s)).with_input_types(
                int).with_output_types(int)))

    assert_that(d, equal_to([10]))
    self.p.run()

  def test_combine_pipeline_type_check_violation_using_methods(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | beam.Create(range(3)).with_output_types(int)
          | (
              'SortJoin' >> beam.CombineGlobally(lambda s: ''.join(sorted(s))).
              with_input_types(str).with_output_types(str)))

    self.assertStartswith(
        e.exception.args[0],
        "Input type hint violation at SortJoin: "
        "expected {}, got {}".format(str, int))

  def test_combine_runtime_type_check_violation_using_methods(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    error_regex = (
        "Runtime type violation detected within " +
        "ParDo\\(SortJoin/KeyWithVoid\\): " +
        "Type-hint for argument: 'v' violated. Expected an instance of " +
        "<class 'str'>, instead found 0, an instance of <class 'int'>.")

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([0]).with_output_types(int)
          | (
              'SortJoin' >> beam.CombineGlobally(lambda s: ''.join(sorted(s))).
              with_input_types(str).with_output_types(str)))
      self.p.run()

  def test_combine_insufficient_type_hint_information(self):
    self.p._options.view_as(TypeOptions).type_check_strictness = 'ALL_REQUIRED'

    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | 'E' >> beam.Create(range(3)).with_output_types(int)
          | 'SortJoin' >> beam.CombineGlobally(lambda s: ''.join(sorted(s)))
          | 'F' >> beam.Map(lambda x: x + 1))

    self.assertStartswith(
        e.exception.args[0],
        'Pipeline type checking is enabled, '
        'however no output type-hint was found for the PTransform '
        'ParDo('
        'SortJoin/CombinePerKey/')

  def test_mean_globally_pipeline_checking_satisfied(self):
    d = (
        self.p
        | 'C' >> beam.Create(range(5)).with_output_types(int)
        | 'Mean' >> combine.Mean.Globally())

    self.assertEqual(float, d.element_type)
    assert_that(d, equal_to([2.0]))
    self.p.run()

  def test_mean_globally_pipeline_checking_violated(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | 'C' >> beam.Create(['test']).with_output_types(str)
          | 'Mean' >> combine.Mean.Globally())
    err_msg = e.exception.args[0]
    assert "CombinePerKey" in err_msg
    assert "Tuple[TypeVariable[K]" in err_msg
    assert "Tuple[None, <class 'str'>" in err_msg

  def test_mean_globally_runtime_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | 'C' >> beam.Create(range(5)).with_output_types(int)
        | 'Mean' >> combine.Mean.Globally())

    self.assertEqual(float, d.element_type)
    assert_that(d, equal_to([2.0]))
    self.p.run()

  def test_mean_globally_runtime_checking_violated(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    error_regex = "Runtime type violation detected"

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | 'C' >> beam.Create(['t', 'e', 's', 't']).with_output_types(str)
          | 'Mean' >> combine.Mean.Globally())
      self.p.run()

  def test_mean_per_key_pipeline_checking_satisfied(self):
    d = (
        self.p
        | beam.Create(range(5)).with_output_types(int)
        | (
            'EvenGroup' >> beam.Map(lambda x: (not x % 2, x)).with_output_types(
                typing.Tuple[bool, int]))
        | 'EvenMean' >> combine.Mean.PerKey())

    self.assertCompatible(typing.Tuple[bool, float], d.element_type)
    assert_that(d, equal_to([(False, 2.0), (True, 2.0)]))
    self.p.run()

  def test_mean_per_key_pipeline_checking_violated(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | beam.Create(map(str, range(5))).with_output_types(str)
          | (
              'UpperPair' >> beam.Map(lambda x:
                                      (x.upper(), x)).with_output_types(
                                          typing.Tuple[str, str]))
          | 'EvenMean' >> combine.Mean.PerKey())
      self.p.run()
    err_msg = e.exception.args[0]
    assert "CombinePerKey(MeanCombineFn)" in err_msg
    assert "requires" in err_msg
    assert "Tuple[TypeVariable[K]" in err_msg
    assert "applied" in err_msg
    assert "Tuple[<class 'str'>, <class 'str'>]" in err_msg

  def test_mean_per_key_runtime_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create(range(5)).with_output_types(int)
        | (
            'OddGroup' >> beam.Map(lambda x:
                                   (bool(x % 2), x)).with_output_types(
                                       typing.Tuple[bool, int]))
        | 'OddMean' >> combine.Mean.PerKey())

    self.assertCompatible(typing.Tuple[bool, float], d.element_type)
    assert_that(d, equal_to([(False, 2.0), (True, 2.0)]))
    self.p.run()

  def test_mean_per_key_runtime_checking_violated(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    error_regex = "Runtime type violation detected"

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create(range(5)).with_output_types(int)
          | (
              'OddGroup' >> beam.Map(lambda x:
                                     (x, str(bool(x % 2)))).with_output_types(
                                         typing.Tuple[int, str]))
          | 'OddMean' >> combine.Mean.PerKey())
      self.p.run()

  def test_count_globally_pipeline_type_checking_satisfied(self):
    d = (
        self.p
        | 'P' >> beam.Create(range(5)).with_output_types(int)
        | 'CountInt' >> combine.Count.Globally())

    self.assertEqual(int, d.element_type)
    assert_that(d, equal_to([5]))
    self.p.run()

  def test_count_globally_runtime_type_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | 'P' >> beam.Create(range(5)).with_output_types(int)
        | 'CountInt' >> combine.Count.Globally())

    self.assertEqual(int, d.element_type)
    assert_that(d, equal_to([5]))
    self.p.run()

  def test_count_perkey_pipeline_type_checking_satisfied(self):
    d = (
        self.p
        | beam.Create(range(5)).with_output_types(int)
        | 'EvenGroup' >> beam.Map(lambda x: (not x % 2, x)).with_output_types(
            typing.Tuple[bool, int])
        | 'CountInt' >> combine.Count.PerKey())

    self.assertCompatible(typing.Tuple[bool, int], d.element_type)
    assert_that(d, equal_to([(False, 2), (True, 3)]))
    self.p.run()

  def test_count_perkey_pipeline_type_checking_violated(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | beam.Create(range(5)).with_output_types(int)
          | 'CountInt' >> combine.Count.PerKey())

    self.assertStartswith(
        e.exception.args[0], 'Input type hint violation at CountInt')

  def test_count_perkey_runtime_type_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create(['t', 'e', 's', 't']).with_output_types(str)
        | 'DupKey' >> beam.Map(lambda x: (x, x)).with_output_types(
            typing.Tuple[str, str])
        | 'CountDups' >> combine.Count.PerKey())

    self.assertCompatible(typing.Tuple[str, int], d.element_type)
    assert_that(d, equal_to([('e', 1), ('s', 1), ('t', 2)]))
    self.p.run()

  def test_count_perelement_pipeline_type_checking_satisfied(self):
    d = (
        self.p
        | beam.Create([1, 1, 2, 3]).with_output_types(int)
        | 'CountElems' >> combine.Count.PerElement())

    self.assertCompatible(typing.Tuple[int, int], d.element_type)
    assert_that(d, equal_to([(1, 2), (2, 1), (3, 1)]))
    self.p.run()

  def test_count_perelement_pipeline_type_checking_violated(self):
    self.p._options.view_as(TypeOptions).type_check_strictness = 'ALL_REQUIRED'

    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | 'f' >> beam.Create([1, 1, 2, 3])
          | 'CountElems' >> combine.Count.PerElement())

    self.assertEqual(
        'Pipeline type checking is enabled, however no output '
        'type-hint was found for the PTransform '
        'Create(f)',
        e.exception.args[0])

  def test_count_perelement_runtime_type_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create([True, True, False, True, True]).with_output_types(bool)
        | 'CountElems' >> combine.Count.PerElement())

    self.assertCompatible(typing.Tuple[bool, int], d.element_type)
    assert_that(d, equal_to([(False, 1), (True, 4)]))
    self.p.run()

  def test_top_of_pipeline_checking_satisfied(self):
    d = (
        self.p
        | beam.Create(range(5, 11)).with_output_types(int)
        | 'Top 3' >> combine.Top.Of(3))

    self.assertCompatible(typing.Iterable[int], d.element_type)
    assert_that(d, equal_to([[10, 9, 8]]))
    self.p.run()

  def test_top_of_runtime_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create(list('testing')).with_output_types(str)
        | 'AciiTop' >> combine.Top.Of(3))

    self.assertCompatible(typing.Iterable[str], d.element_type)
    assert_that(d, equal_to([['t', 't', 's']]))
    self.p.run()

  def test_per_key_pipeline_checking_violated(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | beam.Create(range(100)).with_output_types(int)
          | 'Num + 1' >> beam.Map(lambda x: x + 1).with_output_types(int)
          | 'TopMod' >> combine.Top.PerKey(1))

    self.assertStartswith(
        e.exception.args[0],
        "Input type hint violation at TopMod: expected Tuple[TypeVariable[K], "
        "TypeVariable[V]], got {}".format(int))

  def test_per_key_pipeline_checking_satisfied(self):
    d = (
        self.p
        | beam.Create(range(100)).with_output_types(int)
        | (
            'GroupMod 3' >> beam.Map(lambda x: (x % 3, x)).with_output_types(
                typing.Tuple[int, int]))
        | 'TopMod' >> combine.Top.PerKey(1))

    self.assertCompatible(
        typing.Tuple[int, typing.Iterable[int]], d.element_type)
    assert_that(d, equal_to([(0, [99]), (1, [97]), (2, [98])]))
    self.p.run()

  def test_per_key_runtime_checking_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create(range(21))
        | (
            'GroupMod 3' >> beam.Map(lambda x: (x % 3, x)).with_output_types(
                typing.Tuple[int, int]))
        | 'TopMod' >> combine.Top.PerKey(1))

    self.assertCompatible(
        typing.Tuple[int, typing.Iterable[int]], d.element_type)
    assert_that(d, equal_to([(0, [18]), (1, [19]), (2, [20])]))
    self.p.run()

  def test_sample_globally_pipeline_satisfied(self):
    d = (
        self.p
        | beam.Create([2, 2, 3, 3]).with_output_types(int)
        | 'Sample' >> combine.Sample.FixedSizeGlobally(3))

    self.assertCompatible(typing.Iterable[int], d.element_type)

    def matcher(expected_len):
      def match(actual):
        equal_to([expected_len])([len(actual[0])])

      return match

    assert_that(d, matcher(3))
    self.p.run()

  def test_sample_globally_runtime_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create([2, 2, 3, 3]).with_output_types(int)
        | 'Sample' >> combine.Sample.FixedSizeGlobally(2))

    self.assertCompatible(typing.Iterable[int], d.element_type)

    def matcher(expected_len):
      def match(actual):
        equal_to([expected_len])([len(actual[0])])

      return match

    assert_that(d, matcher(2))
    self.p.run()

  def test_sample_per_key_pipeline_satisfied(self):
    d = (
        self.p
        | (
            beam.Create([(1, 2), (1, 2), (2, 3),
                         (2, 3)]).with_output_types(typing.Tuple[int, int]))
        | 'Sample' >> combine.Sample.FixedSizePerKey(2))

    self.assertCompatible(
        typing.Tuple[int, typing.Iterable[int]], d.element_type)

    def matcher(expected_len):
      def match(actual):
        for _, sample in actual:
          equal_to([expected_len])([len(sample)])

      return match

    assert_that(d, matcher(2))
    self.p.run()

  def test_sample_per_key_runtime_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | (
            beam.Create([(1, 2), (1, 2), (2, 3),
                         (2, 3)]).with_output_types(typing.Tuple[int, int]))
        | 'Sample' >> combine.Sample.FixedSizePerKey(1))

    self.assertCompatible(
        typing.Tuple[int, typing.Iterable[int]], d.element_type)

    def matcher(expected_len):
      def match(actual):
        for _, sample in actual:
          equal_to([expected_len])([len(sample)])

      return match

    assert_that(d, matcher(1))
    self.p.run()

  def test_to_list_pipeline_check_satisfied(self):
    d = (
        self.p
        | beam.Create((1, 2, 3, 4)).with_output_types(int)
        | combine.ToList())

    self.assertCompatible(typing.List[int], d.element_type)

    def matcher(expected):
      def match(actual):
        equal_to(expected)(actual[0])

      return match

    assert_that(d, matcher([1, 2, 3, 4]))
    self.p.run()

  def test_to_list_runtime_check_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | beam.Create(list('test')).with_output_types(str)
        | combine.ToList())

    self.assertCompatible(typing.List[str], d.element_type)

    def matcher(expected):
      def match(actual):
        equal_to(expected)(actual[0])

      return match

    assert_that(d, matcher(['e', 's', 't', 't']))
    self.p.run()

  def test_to_dict_pipeline_check_violated(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      (
          self.p
          | beam.Create([1, 2, 3, 4]).with_output_types(int)
          | combine.ToDict())

    self.assertStartswith(
        e.exception.args[0],
        "Input type hint violation at ToDict: expected Tuple[TypeVariable[K], "
        "TypeVariable[V]], got {}".format(int))

  def test_to_dict_pipeline_check_satisfied(self):
    d = (
        self.p
        | beam.Create([(1, 2),
                       (3, 4)]).with_output_types(typing.Tuple[int, int])
        | combine.ToDict())

    self.assertCompatible(typing.Dict[int, int], d.element_type)
    assert_that(d, equal_to([{1: 2, 3: 4}]))
    self.p.run()

  def test_to_dict_runtime_check_satisfied(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    d = (
        self.p
        | (
            beam.Create([('1', 2),
                         ('3', 4)]).with_output_types(typing.Tuple[str, int]))
        | combine.ToDict())

    self.assertCompatible(typing.Dict[str, int], d.element_type)
    assert_that(d, equal_to([{'1': 2, '3': 4}]))
    self.p.run()

  def test_runtime_type_check_python_type_error(self):
    self.p._options.view_as(TypeOptions).runtime_type_check = True

    error_regex = "object of type 'int' has no len()"

    with self.assertRaisesRegex(Exception, error_regex):
      (
          self.p
          | beam.Create([1, 2, 3]).with_output_types(int)
          | 'Len' >> beam.Map(lambda x: len(x)).with_output_types(int))
      self.p.run()

  def test_pardo_type_inference(self):
    self.assertEqual(int, beam.Filter(lambda x: False).infer_output_type(int))
    self.assertEqual(
        typehints.Tuple[str, int],
        beam.Map(lambda x: (x, 1)).infer_output_type(str))

  def test_gbk_type_inference(self):
    self.assertEqual(
        typehints.Tuple[str, typehints.Iterable[int]],
        beam.GroupByKey().infer_output_type(typehints.KV[str, int]))

  def test_pipeline_inference(self):
    created = self.p | beam.Create(['a', 'b', 'c'])
    mapped = created | 'pair with 1' >> beam.Map(lambda x: (x, 1))
    grouped = mapped | beam.GroupByKey()
    self.assertEqual(str, created.element_type)
    self.assertEqual(typehints.KV[str, int], mapped.element_type)
    self.assertEqual(
        typehints.KV[str, typehints.Iterable[int]], grouped.element_type)

  def test_inferred_bad_kv_type(self):
    with self.assertRaises(typehints.TypeCheckError) as e:
      _ = (
          self.p
          | beam.Create(['a', 'b', 'c'])
          | 'Ungroupable' >> beam.Map(lambda x: (x, 0, 1.0))
          | beam.GroupByKey())

    self.assertStartswith(
        e.exception.args[0],
        "Input type hint violation at GroupByKey: "
        "expected Tuple[TypeVariable[K], TypeVariable[V]], "
        "got Tuple[<class 'str'>, <class 'int'>, <class 'float'>]")

  def test_type_inference_command_line_flag_toggle(self):
    self.p._options.view_as(TypeOptions).pipeline_type_check = False
    x = self.p | 'C1' >> beam.Create([1, 2, 3, 4])
    self.assertIsNone(x.element_type)

    self.p._options.view_as(TypeOptions).pipeline_type_check = True
    x = self.p | 'C2' >> beam.Create([1, 2, 3, 4])
    self.assertEqual(int, x.element_type)

  def test_eager_execution(self):
    doubled = [1, 2, 3, 4] | beam.Map(lambda x: 2 * x)
    self.assertEqual([2, 4, 6, 8], doubled)

  def test_eager_execution_tagged_outputs(self):
    result = [1, 2, 3, 4] | beam.Map(
        lambda x: pvalue.TaggedOutput('bar', 2 * x)).with_outputs('bar')
    self.assertEqual([2, 4, 6, 8], result.bar)
    with self.assertRaises(KeyError,
                           msg='Tag \'foo\' is not a defined output tag'):
      result.foo


@parameterized_class([{'use_subprocess': False}, {'use_subprocess': True}])
class DeadLettersTest(unittest.TestCase):
  @classmethod
  def die(cls, x):
    if cls.use_subprocess:
      os._exit(x)
    else:
      raise ValueError(x)

  @classmethod
  def die_if_negative(cls, x):
    if x < 0:
      cls.die(x)
    else:
      return x

  @classmethod
  def exception_if_negative(cls, x):
    if x < 0:
      raise ValueError(x)
    else:
      return x

  @classmethod
  def die_if_less(cls, x, bound=0):
    if x < bound:
      cls.die(x)
    else:
      return x, bound

  def test_error_messages(self):
    with TestPipeline() as p:
      good, bad = (
          p
          | beam.Create([-1, 10, -100, 2, 0])
          | beam.Map(self.exception_if_negative).with_exception_handling())
      assert_that(good, equal_to([0, 2, 10]), label='CheckGood')
      assert_that(
          bad |
          beam.MapTuple(lambda e, exc_info: (e, exc_info[1].replace(',', ''))),
          equal_to([(-1, 'ValueError(-1)'), (-100, 'ValueError(-100)')]),
          label='CheckBad')

  def test_filters_exceptions(self):
    with TestPipeline() as p:
      good, _ = (
          p
          | beam.Create([-1, 10, -100, 2, 0])
          | beam.Map(self.exception_if_negative).with_exception_handling(
              use_subprocess=self.use_subprocess,
              exc_class=(ValueError, TypeError)))
      assert_that(good, equal_to([0, 2, 10]), label='CheckGood')

    with self.assertRaises(Exception):
      with TestPipeline() as p:
        good, _ = (
            p
            | beam.Create([-1, 10, -100, 2, 0])
            | beam.Map(self.die_if_negative).with_exception_handling(
                use_subprocess=self.use_subprocess,
                exc_class=TypeError))

  def test_tuples(self):

    with TestPipeline() as p:
      good, _ = (
          p
          | beam.Create([(1, 2), (3, 2), (1, -10)])
          | beam.MapTuple(self.die_if_less).with_exception_handling(
              use_subprocess=self.use_subprocess))
      assert_that(good, equal_to([(3, 2), (1, -10)]), label='CheckGood')

  def test_side_inputs(self):

    with TestPipeline() as p:
      input = p | beam.Create([-1, 10, 100])

      assert_that((
          input
          | 'Default' >> beam.Map(self.die_if_less).with_exception_handling(
              use_subprocess=self.use_subprocess)).good,
                  equal_to([(10, 0), (100, 0)]),
                  label='CheckDefault')
      assert_that((
          input
          | 'Pos' >> beam.Map(self.die_if_less, 20).with_exception_handling(
              use_subprocess=self.use_subprocess)).good,
                  equal_to([(100, 20)]),
                  label='PosSideInput')
      assert_that((
          input
          |
          'Key' >> beam.Map(self.die_if_less, bound=30).with_exception_handling(
              use_subprocess=self.use_subprocess)).good,
                  equal_to([(100, 30)]),
                  label='KeySideInput')

  def test_multiple_outputs(self):
    die = type(self).die

    def die_on_negative_even_odd(x):
      if x < 0:
        die(x)
      elif x % 2 == 0:
        return pvalue.TaggedOutput('even', x)
      elif x % 2 == 1:
        return pvalue.TaggedOutput('odd', x)

    with TestPipeline() as p:
      results = (
          p
          | beam.Create([1, -1, 2, -2, 3])
          | beam.Map(die_on_negative_even_odd).with_exception_handling(
              use_subprocess=self.use_subprocess))
      assert_that(results.even, equal_to([2]), label='CheckEven')
      assert_that(results.odd, equal_to([1, 3]), label='CheckOdd')

  def test_params(self):
    die = type(self).die

    def die_if_negative_with_timestamp(x, ts=beam.DoFn.TimestampParam):
      if x < 0:
        die(x)
      else:
        return x, ts

    with TestPipeline() as p:
      good, _ = (
          p
          | beam.Create([-1, 0, 1])
          | beam.Map(lambda x: TimestampedValue(x, x))
          | beam.Map(die_if_negative_with_timestamp).with_exception_handling(
              use_subprocess=self.use_subprocess))
      assert_that(good, equal_to([(0, Timestamp(0)), (1, Timestamp(1))]))

  def test_timeout(self):
    import time
    timeout = 1 if self.use_subprocess else .1

    with TestPipeline() as p:
      good, bad = (
          p
          | beam.Create('records starting with lowercase S are slow'.split())
          | beam.Map(
              lambda x: time.sleep(2.5 * timeout) if x.startswith('s') else x)
          .with_exception_handling(
              use_subprocess=self.use_subprocess, timeout=timeout))
      assert_that(
          good,
          equal_to(['records', 'with', 'lowercase', 'S', 'are']),
          label='CheckGood')
      assert_that(
          bad |
          beam.MapTuple(lambda e, exc_info: (e, exc_info[1].replace(',', ''))),
          equal_to([('starting', 'TimeoutError()'),
                    ('slow', 'TimeoutError()')]),
          label='CheckBad')

  def test_increment_counter(self):
    # Counters are not currently supported for
    # ParDo#with_exception_handling(use_subprocess=True).
    if (self.use_subprocess):
      return

    class CounterDoFn(beam.DoFn):
      def __init__(self):
        self.records_counter = Metrics.counter(self.__class__, 'recordsCounter')

      def process(self, element):
        self.records_counter.inc()

    with TestPipeline() as p:
      _, _ = (
          (p | beam.Create([1,2,3])) | beam.ParDo(CounterDoFn())
          .with_exception_handling(
            use_subprocess=self.use_subprocess, timeout=1))
    results = p.result
    metric_results = results.metrics().query(
        MetricsFilter().with_name("recordsCounter"))
    records_counter = metric_results['counters'][0]

    self.assertEqual(records_counter.key.metric.name, 'recordsCounter')
    self.assertEqual(records_counter.result, 3)

  def test_lifecycle(self):
    die = type(self).die

    class MyDoFn(beam.DoFn):
      state = None

      def setup(self):
        assert self.state is None
        self.state = 'setup'

      def start_bundle(self):
        assert self.state in ('setup', 'finish_bundle'), self.state
        self.state = 'start_bundle'

      def finish_bundle(self):
        assert self.state in ('start_bundle', ), self.state
        self.state = 'finish_bundle'

      def teardown(self):
        assert self.state in ('setup', 'finish_bundle'), self.state
        self.state = 'teardown'

      def process(self, x):
        if x < 0:
          die(x)
        else:
          yield self.state

    with TestPipeline() as p:
      good, _ = (
          p
          | beam.Create([-1, 0, 1, -10, 10])
          | beam.ParDo(MyDoFn()).with_exception_handling(
              use_subprocess=self.use_subprocess))
      assert_that(good, equal_to(['start_bundle'] * 3))

  def test_partial(self):
    if self.use_subprocess:
      self.skipTest('Subprocess and partial mutally exclusive.')

    def die_if_negative_iter(elements):
      for element in elements:
        if element < 0:
          raise ValueError(element)
        yield element

    with TestPipeline() as p:
      input = p | beam.Create([(-1, 1, 11), (2, -2, 22), (3, 33, -3), (4, 44)])

      assert_that((
          input
          | 'Partial' >> beam.FlatMap(
              die_if_negative_iter).with_exception_handling(partial=True)).good,
                  equal_to([2, 3, 33, 4, 44]),
                  'CheckPartial')

      assert_that((
          input
          | 'Complete' >> beam.FlatMap(die_if_negative_iter).
          with_exception_handling(partial=False)).good,
                  equal_to([4, 44]),
                  'CheckComplete')

  def test_threshold(self):
    # The threshold is high enough.
    with TestPipeline() as p:
      _ = (
          p
          | beam.Create([-1, -2, 0, 1, 2, 3, 4, 5])
          | beam.Map(self.die_if_negative).with_exception_handling(
              threshold=0.5, use_subprocess=self.use_subprocess))

    # The threshold is too low enough.
    with self.assertRaisesRegex(Exception, "2 / 8 = 0.25 > 0.1"):
      with TestPipeline() as p:
        _ = (
            p
            | beam.Create([-1, -2, 0, 1, 2, 3, 4, 5])
            | beam.Map(self.die_if_negative).with_exception_handling(
                threshold=0.1, use_subprocess=self.use_subprocess))

    # The threshold is too low per window.
    with self.assertRaisesRegex(Exception, "2 / 2 = 1.0 > 0.5"):
      with TestPipeline() as p:
        _ = (
            p
            | beam.Create([-1, -2, 0, 1, 2, 3, 4, 5])
            | beam.Map(lambda x: TimestampedValue(x, x))
            | beam.Map(self.die_if_negative).with_exception_handling(
                threshold=0.5,
                threshold_windowing=window.FixedWindows(10),
                use_subprocess=self.use_subprocess))


class TestPTransformFn(TypeHintTestCase):
  def test_type_checking_fail(self):
    @beam.ptransform_fn
    def MyTransform(pcoll):
      return pcoll | beam.ParDo(lambda x: [x]).with_output_types(str)

    p = TestPipeline()
    with self.assertRaisesRegex(beam.typehints.TypeCheckError,
                                r'expected.*int.*got.*str'):
      _ = (p | beam.Create([1, 2]) | MyTransform().with_output_types(int))

  def test_type_checking_success(self):
    @beam.ptransform_fn
    def MyTransform(pcoll):
      return pcoll | beam.ParDo(lambda x: [x]).with_output_types(int)

    with TestPipeline() as p:
      _ = (p | beam.Create([1, 2]) | MyTransform().with_output_types(int))

  def test_type_hints_arg(self):
    # Tests passing type hints via the magic 'type_hints' argument name.
    @beam.ptransform_fn
    def MyTransform(pcoll, type_hints, test_arg):
      self.assertEqual(test_arg, 'test')
      return (
          pcoll
          | beam.ParDo(lambda x: [x]).with_output_types(
              type_hints.output_types[0][0]))

    with TestPipeline() as p:
      _ = (p | beam.Create([1, 2]) | MyTransform('test').with_output_types(int))


class PickledObject(object):
  def __init__(self, value):
    self.value = value


if __name__ == '__main__':
  unittest.main()
