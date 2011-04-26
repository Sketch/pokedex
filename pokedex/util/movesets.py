#! /usr/bin/env python
# Encoding: UTF-8

import sys
import argparse
import itertools
from collections import defaultdict, namedtuple

from sqlalchemy.orm import aliased
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql.expression import not_, and_, or_

from pokedex.db import connect, tables, util

from pokedex.util import querytimer
from pokedex.util.astar import a_star, Node

###
### Illegal Moveset exceptions
###

class IllegalMoveCombination(ValueError): pass
class TooManyMoves(IllegalMoveCombination): pass
class NoMoves(IllegalMoveCombination): pass
class MovesNotLearnable(IllegalMoveCombination): pass
class NoParent(IllegalMoveCombination): pass
class TargetExcluded(IllegalMoveCombination): pass

###
### Generic helpers
###

def powerset(iterable):
    # recipe from: http://docs.python.org/library/itertools.html
    "powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3) (1,2,3)"
    s = list(iterable)
    return itertools.chain.from_iterable(itertools.combinations(s, r)
            for r in range(len(s)+1))

###
### Search information object
###

class MovesetSearch(object):
    def __init__(self, session, pokemon, version, moves, level=100, costs=None,
            exclude_versions=(), exclude_pokemon=(), debug=False):

        self.generator = self.error = None

        if not moves:
            self.error = NoMoves('No moves specified.')
            return
        elif len(moves) > 4:
            self.error = NoMoves('Too many moves specified.')
            return

        self.debug = debug

        self.session = session

        self.sketch = util.get(session, tables.Move, identifier=u'sketch').id
        self.no_eggs_group = util.get(session, tables.EggGroup,
                identifier=u'no-eggs').id
        self.ditto_group = util.get(session, tables.EggGroup,
                identifier=u'ditto').id

        if costs is None:
            self.costs = default_costs
        else:
            self.costs = costs

        self.load_pokemon()

        self.excluded_families = frozenset(p.evolution_chain_id
                for p in exclude_pokemon)

        if pokemon:
            self.goal_evolution_chain = pokemon.evolution_chain_id
            if self.goal_evolution_chain in self.excluded_families:
                self.error = TargetExcluded('The target pokemon was excluded.')
                return
        else:
            self.goal_evolution_chain = None

        if debug:
            print 'Specified moves:', [move.id for move in moves]

        self.goal_moves = frozenset(move.id for move in moves)
        self.goal_version_group = version.version_group_id

        # Fill self.generation_id_by_version_group
        self.load_version_groups(version.version_group_id,
                [v.version_group_id for v in exclude_versions])

        self.pokemon_moves = defaultdict(  # key: pokemon
                lambda: defaultdict(  # key: version_group
                    lambda: defaultdict(  # key: move
                        lambda: defaultdict(  # key: method
                            list))))  # ordered list of (level, cost)
        self.movepools = defaultdict(dict)  # evo chain -> move -> best cost
        self.learnpools = defaultdict(set)  # evo chain -> move, w/o egg moves

        easy_moves, non_egg_moves = self.load_pokemon_moves(
                self.goal_evolution_chain, 'family')

        hard_moves = self.goal_moves - easy_moves
        self.egg_moves = self.goal_moves - non_egg_moves
        if hard_moves:
            # Have to breed!
            self.load_pokemon_moves(self.goal_evolution_chain, 'others')

        self.construct_breed_graph()

        self.find_duplicate_versions()

        kwargs = dict()
        if debug:
            self._astar_debug_notify_counter = 0
            kwargs['notify'] = self.astar_debug_notify
        self.generator = InitialNode(self).find_all_paths(**kwargs)

    def load_version_groups(self, version, excluded):
        """Load generation_id_by_version_group
        """
        query = self.session.query(tables.VersionGroup.id,
                tables.VersionGroup.generation_id)
        query = query.join(tables.Version.version_group)
        if excluded:
            query = query.filter(not_(tables.VersionGroup.id.in_(excluded)))
        self.generation_id_by_version_group = dict(query)
        def expand(v2):
            for v1 in self.generation_id_by_version_group:
                if self.trade_cost(v1, v2):
                    yield 0, None, v1
        def is_goal(v):
            return True
        goal = self.goal_version_group
        filtered_map = {goal: self.generation_id_by_version_group[goal]}
        for result in a_star(self.goal_version_group, expand, is_goal):
            for cost, transition, version in result:
                filtered_map[version] = (
                        self.generation_id_by_version_group[version])
        self.generation_id_by_version_group = filtered_map
        if self.debug:
            print 'Excluded version groups:', excluded
            print 'Trade cost table:'
            print '%03s' % '',
            for g1 in sorted(self.generation_id_by_version_group):
                print '%03s' % g1,
            print
            for g1 in sorted(self.generation_id_by_version_group):
                print '%03s' % g1,
                for g2 in sorted(self.generation_id_by_version_group):
                    print '%03s' % (self.trade_cost(g1, g2) or '---'),
                print

    def load_pokemon_moves(self, evolution_chain, selection):
        """Load pokemon_moves, movepools, learnpools, smeargle_families

        `selection`:
            'family' for loading only pokemon in evolution_chain
            'others' for loading only pokemon NOT in evolution_chain

        Returns: (easy_moves, non_egg_moves)
        If `selection` == 'family':
            easy_moves is a set of moves that are easier to obtain than by
                breeding
            non_egg_moves is a set of moves that don't require breeding
        Otherwise, these are empty sets.
        """
        if self.debug:
            print 'Loading moves, c%s %s' % (evolution_chain, selection)
        query = self.session.query(
                tables.PokemonMove.pokemon_id,
                tables.PokemonMove.move_id,
                tables.PokemonMove.version_group_id,
                tables.PokemonMoveMethod.identifier,
                tables.PokemonMove.level,
                tables.Pokemon.evolution_chain_id,
            )
        query = query.join(tables.PokemonMove.pokemon)
        query = query.filter(tables.PokemonMoveMethod.id == 
                tables.PokemonMove.pokemon_move_method_id)
        query = query.filter(tables.PokemonMove.version_group_id.in_(
                set(self.generation_id_by_version_group)))
        query = query.filter(or_(
                    tables.PokemonMove.level > 100,  # XXX: Chaff?
                    tables.PokemonMove.move_id.in_(self.goal_moves),
                    tables.PokemonMove.move_id == self.sketch,
                    tables.PokemonMove.move_id.in_(
                            self.evolution_moves.values()),
                ))
        if self.excluded_families:
            query = query.filter(not_(tables.Pokemon.evolution_chain_id.in_(
                    self.excluded_families)))
        if evolution_chain:
            if selection == 'family':
                query = query.filter(tables.Pokemon.evolution_chain_id == (
                        evolution_chain))
            elif selection == 'others':
                query = query.filter(tables.Pokemon.evolution_chain_id != (
                        evolution_chain))
        query = query.order_by(tables.PokemonMove.level)
        easy_moves = set()
        non_egg_moves = set()
        self.smeargle_families = set()
        costs = self.costs
        movepools = self.movepools
        learnpools = self.learnpools
        sketch_cost = costs['sketch']
        breed_cost = costs['breed']
        for pokemon, move, vg, method, level, chain in query:
            if move in self.goal_moves:
                if method == 'level-up':
                    cost = costs['level-up']
                else:
                    gen = self.generation_id_by_version_group[vg]
                    if method == 'machine' and gen < 5:
                        cost = costs['machine-once']
                    elif method == 'tutor' and gen == 3:
                        cost = costs['tutor-once']
                    elif method == 'egg':
                        cost = costs['breed']
                    else:
                        cost = costs[method]
                movepools[chain][move] = min(
                        movepools[chain].get(move, cost), cost)
                if method != 'egg':
                    learnpools[chain].add(move)
                    non_egg_moves.add(move)
                    if cost < breed_cost:
                        easy_moves.add(move)
            elif move == self.sketch:
                cost = sketch_cost
                self.smeargle_families.add(self.evolution_chains[pokemon])
            else:
                # An evolution move. We need to use it anyway if we need
                # the evolution, so the cost can be an arbitrary positive
                # number. But, do check if this family actually needs the move.
                evolution_chain = self.evolution_chains[pokemon]
                if move != self.evolution_moves.get(evolution_chain):
                    continue
                cost = 1
            self.pokemon_moves[pokemon][vg][move][method].append((level, cost))
        if self.debug and selection == 'family':
            print 'Easy moves:', sorted(easy_moves)
            print 'Non-egg moves:', sorted(non_egg_moves)
        if self.debug:
            print 'Smeargle families:', sorted(self.smeargle_families)
        return easy_moves, non_egg_moves

    def trade_cost(self, version_group_from, version_group_to, *thing_generations):
        """Return cost of trading between versions, None if impossibble

        `thing_generations` should be the generation IDs of the pokemon and
        moves being traded.
        """
        # XXX: this ignores HM transfer restrictions
        gen_from = self.generation_id_by_version_group[version_group_from]
        gen_to = self.generation_id_by_version_group[version_group_to]
        if gen_from == gen_to:
            return self.costs['trade']
        elif any(gen > gen_to for gen in thing_generations):
            return None
        elif gen_from in (1, 2):
            if gen_to in (1, 2):
                return self.costs['trade']
            else:
                return None
        elif gen_to in (1, 2):
            return None
        elif gen_from > gen_to:
            return None
        elif gen_from < gen_to - 1:
            return None
        else:
            return self.costs['trade'] + self.costs['transfer']

    def load_pokemon(self):
        """Load pokemon breed groups and evolutions

        self.egg_groups: maps evolution chains to their sorted egg groups
            (wil contain empty tuple for no-eggs or ditto)
        self.evolution_chains: maps pokemon to their evolution chains
        self.pokemon_by_evolution_chain: maps evolution chains to their pokemon
        self.unbreedable: set of unbreedable pokemon

        self.evolution_parents[pokemon] = the pre-evolved form
        self.evolutions[pokemon] = list of (trigger, move, level, child)
        self.evolution_moves[evolution_chain] = move required for evolution
        self.babies[egg_group_id] = set of baby pokemon
        """
        eg1 = tables.PokemonEggGroup
        eg2 = aliased(tables.PokemonEggGroup)
        query = self.session.query(
                tables.Pokemon.id,
                tables.Pokemon.evolution_chain_id,
                tables.Pokemon.evolves_from_pokemon_id,
                eg1.egg_group_id,
                eg2.egg_group_id,
                tables.EvolutionChain.baby_trigger_item_id,
            )
        query = query.join(tables.Pokemon.evolution_chain)
        query = query.join((eg1, eg1.pokemon_id == tables.Pokemon.id))
        query = query.outerjoin((eg2, and_(
                eg2.pokemon_id == tables.Pokemon.id,
                eg1.egg_group_id < eg2.egg_group_id,
            )))
        bad_groups = (self.no_eggs_group, self.ditto_group)
        unbreedable = set()
        self.evolution_parents = dict()
        self.egg_groups = defaultdict(tuple)
        self.evolution_chains = dict()
        self.pokemon_by_evolution_chain = defaultdict(set)
        self.babies = defaultdict(set)
        item_baby_chains = set()  # evolution chains with baby-trigger items
        for pokemon, evolution_chain, parent, g1, g2, baby_item in query:
            groups = (g1, g2) if g2 else (g1, )
            if g1 in bad_groups:
                unbreedable.add(pokemon)
            else:
                if len(self.egg_groups.get(evolution_chain, ())) <= len(groups):
                    self.egg_groups[evolution_chain] = groups
            self.evolution_chains[pokemon] = evolution_chain
            self.pokemon_by_evolution_chain[evolution_chain].add(pokemon)
            if parent:
                self.evolution_parents[pokemon] = parent
                if baby_item:
                    item_baby_chains.add(evolution_chain)
            else:
                for group in groups:
                    self.babies[group].add(pokemon)
        self.unbreedable = frozenset(unbreedable)

        self.evolutions = defaultdict(set)
        self.evolution_moves = dict()
        query = self.session.query(
                tables.PokemonEvolution.evolved_pokemon_id,
                tables.EvolutionTrigger.identifier,
                tables.PokemonEvolution.known_move_id,
                tables.PokemonEvolution.minimum_level,
            )
        query = query.join(tables.PokemonEvolution.trigger)
        for child, trigger, move, level in query:
            self.evolutions[self.evolution_parents[child]].add(
                    (trigger, move, level, child))
            if move:
                self.evolution_moves[self.evolution_chains[child]] = move

        if self.debug:
            print 'Loaded %s pokemon: %s evo; %s families: %s breedable' % (
                    len(self.evolution_chains),
                    len(self.pokemon_by_evolution_chain),
                    len(self.egg_groups),
                    len(self.evolutions),
                )
            print 'Evolution moves: %s' % self.evolution_moves

        # Chains with item-triggered alternate babies
        for item_baby_chain in item_baby_chains:
            for item_baby in self.pokemon_by_evolution_chain[item_baby_chain]:
                if item_baby not in self.evolution_parents:
                    for regular_baby in self.evolutions[item_baby]:
                        for group in self.egg_groups[item_baby_chain]:
                            self.babies[group].add(pokemon)

    def construct_breed_graph(self):
        """Fills breeds_required

        breeds_required[egg_group][moveset] = minimum number of breeds needed
            from a pokemon in this group with this moveset to the goal pokemon
            with the goal moveset.
            The score cannot get lower by learning new moves, only by breeding.
            If missing, breeding or raising the pokemon won't do any good.
        Exceptions:
            For pokemon in the target family, breeds_required doesn't apply.
            For the empty moveset just check if any moveset is worthwhile (i.e.
            bool(breeds_required[egg_group])).
        """

        # Part I. Determining what moves can be passed/learned

        # eg1_movepools[egg_group_id] = set of moves passable by pkmn in that group
        eg1_movepools = defaultdict(set)
        # eg2_movepools[b_g_id1, b_g_id2] = ditto for pkmn in *both* groups
        eg2_movepools = defaultdict(set)
        # non_egg_pools = as eg1_movepools but for *learnable* moves
        learn_pools = defaultdict(set)

        goal_egg_groups = self.egg_groups[self.goal_evolution_chain]
        all_groups = set()

        for family, groups in self.egg_groups.iteritems():
            if not groups:
                continue
            if family == self.goal_evolution_chain:
                continue
            elif family in self.smeargle_families:
                pool = self.goal_moves
            else:
                pool = self.movepools[family]
                pool = set(pool) & self.goal_moves
            learnpool = self.learnpools[family] & pool
            for group in groups:
                eg1_movepools[group].update(pool)
                learn_pools[group].update(learnpool)
                all_groups.add(group)
            if len(groups) >= 2:
                eg2_movepools[groups].update(pool)

        if self.debug:
            print 'Egg group summary:'
            for group in sorted(all_groups):
                print "%2s can pass: %s" % (group, sorted(eg1_movepools[group]))
                if learn_pools[group] != eg1_movepools[group]:
                    print "  but learn: %s" % sorted(learn_pools[group])
            for g2 in sorted(all_groups):
                for g1 in sorted(all_groups):
                    if eg2_movepools[g1, g2]:
                        print " %2s/%2s pass: %s" % (g1, g2, sorted(eg2_movepools[g1, g2]))
            print 'Goal groups:', goal_egg_groups

        # Part II. Determining which moves are worthwhile to pass

        # We want *all* paths, not just shortest ones, so use DFS.
        breeds_required = defaultdict(dict)
        def handle(group, moves, path):
            """
            group: the group of the parent
            moves: moves the parent should pass down
            path: previously visited groups - to prevent cycles
            """
            if not moves:
                # No more moves needed to pass down: success!
                return True
            if breeds_required[group].get(moves, 999) <= len(path):
                # Already done
                return True
            success = False
            # Breed some more
            path = path + (group, )
            for new_group in all_groups.difference(path):
                new_groups = tuple(sorted([group, new_group]))
                # Can we pass down all the requested moves?
                if moves.issubset(eg1_movepools[new_group]):
                    # Learn some of the moves: they don't have to be passed to us
                    for learned in powerset(moves & learn_pools[new_group]):
                        new_moves = moves.difference(learned)
                        local_success = handle(new_group, new_moves, path)
                        # If this chain eventually ended up being successful,
                        # it means that it is useful to pass this moveset
                        # to this group.
                        if local_success:
                            breeds_required[group][moves] = min(breeds_required[group].get(moves, 999), len(path) - 1)
                            success = True
            return success
        for group in goal_egg_groups:
            handle(group, self.goal_moves, ())
            for moves in powerset(self.goal_moves.difference(self.egg_moves)):
                if moves:
                    breeds_required[group][frozenset(moves) | self.egg_moves] = 1
        self.breeds_required = breeds_required

        if self.debug:
            for group, movesetlist in breeds_required.items():
                print 'From egg group', group
                for moveset, cost in movesetlist.items():
                    print "   %s breeds with %s" % (cost, sorted(moveset))

    def find_duplicate_versions(self):
        """Fill `duplicate_versions`

        duplicate_versions[pokemon][version_group] = set of version groups that
            are identical as far as the pokemon learning those moves is
            concerned, and are in the same generation.
            Thus, trading between them is unnecessary.
        """
        self.duplicate_versions = dict()

        counter = 0
        for pokemon, vg_moves in self.pokemon_moves.items():
            dupes = self.duplicate_versions[pokemon] = dict()
            last = None
            last_moves = None
            last_gen = None
            for version_group, moves in vg_moves.items():
                gen = self.generation_id_by_version_group[version_group]
                if gen == last_gen and moves == last_moves:
                    last.add(version_group)
                    dupes[version_group] = last
                    counter += 1
                else:
                    last = set([version_group])
                    dupes[version_group] = last
                    last_moves = moves
                    last_gen = gen

        if self.debug:
            print 'Deduplicated %s version groups' % counter

    def astar_debug_notify(self, cost, node, setsize, heapsize):
        counter = self._astar_debug_notify_counter
        if counter % 100 == 0:
            print 'A* iteration %s, cost %s; remaining: %s(%s)     \r' % (
                    counter, cost, setsize, heapsize),
        self._astar_debug_notify_counter += 1

    def __iter__(self):
        if self.generator:
            return self.generator
        else:
            raise self.error

###
### Costs
###

default_costs = {
    # Costs for learning a move in verious ways
    'level-up': 20,  # The normal way
    'machine': 40,  # Machines are slightly inconvenient.
    'machine-once': 2000,  # before gen. 5, TMs only work once. Avoid.
    'tutor': 60,  # Tutors are slightly more inconvenient than TMs – can't carry them around
    'tutor-once': 2100,  # gen III: tutors only work once (well except Emerald frontier ones)

    # For technical reasons, 'sketch' is also used for learning Sketch and
    # evolution-inducing moves by normal means, if they aren't included in the
    # target moveset.
    # So the actual cost of a sketched move will be double this number.
    'sketch': 5,  # Cheap. Exclude Smeargle if you think it's too cheap.

    # Gimmick moves – we need to use this method to learn the move anyway,
    # so make a big-ish dent in the score if missing
    'stadium-surfing-pikachu': 100,
    'light-ball-egg': 100,  #  … 

    # Ugh... I don't know?
    'colosseum-purification': 100,
    'xd-shadow': 100,
    'xd-purification': 100,
    'form-change': 100,

    # Other actions.
    # Breeding should cost more than 3 times than a lv-up/machine/tutor move.
    'evolution': 100,  # We have to do this anyway, usually.
    'evolution-delayed': 50,  # *in addition* to evolution. Who wants to mash B on every level.
    'breed': 400,  # Breeding's a pain.
    'trade': 200,  # Trading's a pain, but not as much as breeding.
    'transfer': 200,  # *in addition* to trade. For one-way cross-generation transfers
    'forget': 300,  # Deleting a move. (Not needed unless deleting an evolution move.)
    'relearn': 150,  # Also a pain, though not as big as breeding.
    'per-level': 1,  # Prefer less grinding. This is for all lv-ups but the final “grow”
}

###
### Search space transitions
###

class Action(object):
    pass

class StartAction(Action, namedtuple('StartAcion', 'pokemon version_group')):
    keyword = 'start'

class LearnAction(Action, namedtuple('LearnAction', 'move method')):
    keyword = 'start'

class ForgetAction(Action, namedtuple('ForgetAction', 'move')):
    keyword = 'forget'

###
### Search space nodes
###

class InitialNode(Node, namedtuple('InitialNode', 'search')):
    def expand(self):
        search = self.search
        for pokemon, version_groups in search.pokemon_moves.items():
            egg_groups = search.egg_groups[search.evolution_chains[pokemon]]
            if any(search.breeds_required[group] for group in egg_groups):
                for version_group in version_groups:
                    yield 0, StartAction(pokemon, version_group), PokemonNode(
                        search, pokemon, 0, version_group, frozenset(), False)

class PokemonNode(Node, namedtuple('PokemonNode',
        'search pokemon level version_group moves new_level')):
    def expand(self):
        #print 'expand', self
        if not self.moves:
            # Learn something first
            # (other expand_* may rely on there being a move)
            return self.expand_learn()
        elif len(self.moves) < 4:
            return self.expand_learn()
        else:
            return self.expand_forget()
        return ()

    def expand_learn(self):
        search = self.search
        moves = search.pokemon_moves[self.pokemon][self.version_group]
        for move, methods in moves.items():
            if move in self.moves:
                continue
            for method, levels_costs in methods.items():
                if method == 'level-up':
                    for level, cost in levels_costs:
                        level_difference = level - self.level
                        if level_difference > 0 or (
                                level_difference == 0 and self.new_level):
                            cost += level_difference * search.costs['per-level']
                            yield self._learn(move, method, cost,
                                level=level, new_level=True)
                        else:
                            yield self._learn(move, 'relearn',
                                search.costs['relearn'], new_level=False)
                elif method in 'machine tutor'.split():
                    for level, cost in levels_costs:
                        yield self._learn(move, method, cost, new_level=False)
                elif method == 'egg':
                    # ignored here
                    pass
                elif method == 'light-ball-egg':
                    if self.level == 0:
                        for level, cost in levels_costs:
                            yield self._learn(move, method, cost)
                elif method == 'stadium-surfing-pikachu':
                    for level, cost in levels_costs:
                        yield self._learn(move, method, cost, new_level=False)
                else:
                    raise ValueError('Unknown move method %s' % method)

    def _learn(self, move, method, cost, **kwargs):
        kwargs['moves'] = self.moves.union([move])
        return cost, LearnAction(move, method), self._replace(**kwargs)

    def expand_forget(self):
        cost = self.search.costs['forget']
        for move in self.moves:
            yield cost, ForgetAction(move), self._replace(
                    moves=self.moves.difference([move]), new_level=False)

###
### CLI interface
###

def main(argv):
    parser = argparse.ArgumentParser(description=
        'Find out if the specified moveset is valid, and provide a suggestion '
        'on how to obtain it.')

    parser.add_argument('pokemon', metavar='POKEMON', type=unicode,
        help='Pokemon to check the moveset for')

    parser.add_argument('move', metavar='MOVE', type=unicode, nargs='*',
        help='Moves in the moveset')

    parser.add_argument('-l', '--level', metavar='LV', type=int, default=100,
        help='Level of the pokemon')

    parser.add_argument('-v', '--version', metavar='VER', type=unicode,
        default='black',
        help='Version to search in.')

    parser.add_argument('-V', '--exclude-version', metavar='VER', type=unicode,
        action='append', default=[],
        help='Versions to exclude (along with their '
            'counterparts, if any, e.g. `black` will also exclude White).')

    parser.add_argument('-P', '--exclude-pokemon', metavar='PKM', type=unicode,
        action='append', default=[],
        help='Pokemon to exclude (along with their families, e.g. `pichu` '
            'will also exclude Pikachu and Raichu).')

    parser.add_argument('-d', '--debug', action='append_const', const=1,
        default=[],
        help='Output timing and debugging information (can be specified more '
            'than once).')

    args = parser.parse_args(argv)
    args.debug = len(args.debug)

    if args.debug:
        print 'Connecting'

    session = connect(engine_args={'echo': args.debug > 1})

    if args.debug:
        print 'Parsing arguments'

    def _get_list(table, idents, name):
        result = []
        for ident in idents:
            try:
                result.append(util.get(session, table, identifier=ident))
            except NoResultFound:
                print>>sys.stderr, ('%s %s not found. Please use '
                        'the identifier.' % (name, ident))
                return 2
        return result

    pokemon = _get_list(tables.Pokemon, [args.pokemon], 'Pokemon')[0]
    moves = _get_list(tables.Move, args.move, 'Move')
    version = _get_list(tables.Version, [args.version], 'Version')[0]
    excl_versions = _get_list(tables.Version, args.exclude_version, 'Version')
    excl_pokemon = _get_list(tables.Pokemon, args.exclude_pokemon, 'Pokemon')

    if args.debug:
        print 'Starting search'

    search = MovesetSearch(session, pokemon, version, moves, args.level,
        exclude_versions=excl_versions, exclude_pokemon=excl_pokemon,
        debug=args.debug)

    if args.debug:
        print 'Setup done'

    for result in search:
        print
        print result

    print

    if args.debug:
        print 'Done'

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
